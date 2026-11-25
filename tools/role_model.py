"""Request a reviewed local-model role from Orchestration."""
import json
import os
import sys
import urllib.request
from uuid import uuid4

def request(arguments):
    endpoint = os.environ['ORCHESTRATION_URL'].rstrip('/')
    token = os.environ['ORCHESTRATION_CONTROL_TOKEN']
    rid = uuid4().hex
    req = urllib.request.Request(endpoint + '/v1/control', method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        data=json.dumps({'schema': 'orchestration-control/1', 'request_id': rid,
                         'operation': 'catalog.role', 'arguments': arguments}).encode())
    with urllib.request.urlopen(req, timeout=30) as response:
        value = json.load(response)
    if value.get('schema') != 'orchestration-control/1' or value.get('request_id') != rid or not value.get('ok'):
        raise RuntimeError('Orchestration refused model role resolution')
    return value['result']

def resolve(role='single_1060'):
    return request({'role': role})

def resolve_model(model):
    return request({'model': model})

if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    result = resolve(args[0] if args else 'single_1060')
    print(json.dumps(result) if '--json' in sys.argv else result['model'])
