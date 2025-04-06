# backend.py - API Flask pour gérer l'exécution du code
from flask import Flask, request, jsonify
from flask_cors import CORS
import docker
import uuid
import os
import json
import tempfile

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": ["http://192.168.1.55:80","http://192.168.1.55:443","http://127.0.0.1:5500", "http://localhost:5500", "https://an4lisateur.freeboxos.fr:53000","http://an4lisateur.freeboxos.fr:54000/"]}})

# Client Docker
client = docker.from_env()

# Configuration
DOCKER_IMAGE = "python:3.9-slim"  # Image Docker à utiliser
TIMEOUT = 5  # Timeout en secondes pour l'exécution
MAX_MEMORY = "500m"  # Limite de mémoire

@app.route('/execute', methods=['POST'])
def execute_code():
    try:
        data = request.json
        code = data.get('code', '')
        challenge_id = data.get('challenge_id', '')
        tests = data.get('tests', [])
        
        # Créer un ID unique pour cette exécution
        execution_id = str(uuid.uuid4())
        
        # Créer un fichier temporaire avec le code
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False) as f:
            code_file = f.name
            f.write(code.encode('utf-8'))
        
        # Créer un fichier de test
        with tempfile.NamedTemporaryFile(suffix='.py', delete=False) as f:
            test_file = f.name
            test_code = generate_test_code(tests, os.path.basename(code_file))
            f.write(test_code.encode('utf-8'))
        
        try:
            # Exécuter le code dans Docker
            container = client.containers.run(
                DOCKER_IMAGE,
                command=f"python -u {os.path.basename(test_file)}",
                volumes={
                    os.path.dirname(code_file): {'bind': '/code', 'mode': 'ro'},
                },
                working_dir="/code",
                remove=True,
                detach=True,
                mem_limit=MAX_MEMORY,
                network_disabled=True,  # Désactiver l'accès réseau
                cap_drop=['ALL'],  # Supprimer toutes les capacités
                security_opt=["no-new-privileges:true"],  # Empêcher l'escalade de privilèges
            )
            
            # Attendre le résultat avec timeout
            result = container.wait(timeout=TIMEOUT)
            logs = container.logs().decode('utf-8')
            
            # Nettoyer
            try:
                container.remove(force=True)
            except:
                pass
                
            # Analyser les résultats
            exit_code = result.get('StatusCode', -1)
            if exit_code != 0:
                return jsonify({
                    'success': False,
                    'message': 'Erreur lors de l\'exécution',
                    'output': logs,
                    'test_results': []
                })
            
            # Analyser la sortie pour extraire les résultats des tests
            test_results, general_output = parse_test_results(logs)
            
            return jsonify({
                'success': True,
                'output': general_output,
                'test_results': test_results,
                'all_passed': all(test.get('passed') for test in test_results)
            })
            
        finally:
            # Nettoyer les fichiers temporaires
            try:
                os.unlink(code_file)
                os.unlink(test_file)
            except:
                pass
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Erreur serveur: {str(e)}',
            'output': '',
            'test_results': []
        })

def generate_test_code(tests, code_filename):
    """Génère le code de test à exécuter dans Docker"""
    # Prétraiter tests pour les booléens
    processed_tests = []
    for test in tests:
        processed_test = test.copy()
        # Convertir les booléens en leur équivalent Python
        if isinstance(test['expected'], bool):
            processed_test['expected'] = "True" if test['expected'] else "False"
        processed_tests.append(processed_test)
    
    test_code = """
import sys
import json
import traceback
from io import StringIO

# Fonction pour comparer les résultats en tenant compte des types
def compare_values(result, expected):
    # Gérer le cas des booléens
    if isinstance(expected, bool) or expected in ["True", "False"]:
        if expected in ["True", "False"]:
            expected = expected == "True"
        return bool(result) == expected
    # Comparaison normale
    return result == expected

# Charger le code utilisateur
try:
    with open('{code_file}', 'r') as f:
        user_code = f.read()
        
    # Exécuter le code utilisateur dans un environnement isolé
    user_globals = {{}}
    
    # Capturer la sortie générale du code utilisateur
    original_stdout = sys.stdout
    general_output = StringIO()
    sys.stdout = general_output
    
    exec(user_code, user_globals)
    
    # Restaurer stdout pour les tests
    sys.stdout = original_stdout
    
    # Exécuter les tests avec une capture individuelle pour chaque test
    test_results = []
    
    for test in {tests_json}:
        test_result = {{'code': test['code'], 'passed': False}}
        
        # Capturer la sortie spécifique à ce test
        test_output = StringIO()
        sys.stdout = test_output
        
        try:
            result = eval(test['code'], user_globals)
            expected = test['expected']
            
            # Vérifier l'égalité en tenant compte des types
            test_result['passed'] = compare_values(result, expected)
            
            # Convertir en chaîne pour l'affichage
            test_result['result'] = str(result)
            test_result['expected'] = str(expected)
        except Exception as e:
            test_result['error'] = str(e)
            test_result['traceback'] = traceback.format_exc()
        
        # Récupérer la sortie pour ce test
        sys.stdout = original_stdout
        test_result['output'] = test_output.getvalue()
        
        test_results.append(test_result)
            
    # Afficher les résultats
    print(json.dumps({{
        'general_output': general_output.getvalue(), 
        'test_results': test_results
    }}))
    
except Exception as e:
    sys.stdout = sys.__stdout__  # S'assurer que stdout est restauré
    print(json.dumps({{'error': str(e), 'traceback': traceback.format_exc()}}))
    sys.exit(1)
""".format(
        code_file=code_filename,
        tests_json=json.dumps(processed_tests)
    )
    return test_code

def parse_test_results(logs):
    """Analyse la sortie pour extraire les résultats des tests"""
    try:
        # Essayer de parser la dernière ligne comme JSON
        lines = logs.strip().split('\n')
        for line in reversed(lines):
            try:
                result_data = json.loads(line)
                if isinstance(result_data, dict) and 'test_results' in result_data:
                    general_output = result_data.get('general_output', '')
                    return result_data['test_results'], general_output
            except:
                continue
        
        # Si on ne trouve pas de JSON, retourner l'erreur
        return [{'code': 'execution', 'passed': False, 'error': logs}], ''
    except:
        return [{'code': 'parsing', 'passed': False, 'error': 'Erreur lors de l\'analyse des résultats'}], ''

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)
