import json
import os
from pathlib import Path
import httpx
from llm_gateway.orchestration_client import OrchestrationClient

PREFIXES = {'qwen3.6:35b-a3b-xl': 'shared-35b-', 'nemotron-3.5-bolt:74b-a9b': 'shared-nemo-'}

def fake_owner(lib=None, state=None, rig=None):
    calls = []
    async def respond(request):
        envelope = json.loads(request.content)
        calls.append(envelope)
        op, args = envelope['operation'], envelope['arguments']
        result, error = {}, None
        rows = {p['id']: p for p in json.loads(Path(lib).read_text())['remote_profiles']} if lib else {}
        status = await rig.status() if rig else json.loads(Path(state).read_text()) if state else {'profiles': []}
        if op == 'snapshot':
            result = {'model_prefix': 'gpu-rig', 'profiles': rows, 'chat_offers': rows, 'shared_profiles': {}, 'route_prefixes': PREFIXES,
                      'summary': {'models': [{'model': 'fixture', 'status': 'active', 'scores': {}}], 'roles': {}, 'key_tests': []}}
        elif op == 'rig.status':
            if os.getenv('FAKE_RIG_SSH') == 'down':
                error = {'status': 502, 'message': 'Orchestration unavailable'}
            else:
                result = status
        elif op == 'route.prepare':
            mode = os.getenv('FAKE_RIG_START', 'ok')
            if os.getenv('FAKE_RIG_SSH') == 'down':
                error = {'status': 502, 'message': 'Orchestration unavailable'}
            elif args.get('ready_only') and not any(p.get('id') == args['profile'] and p.get('state') == 'ready' and p.get('model') == rows[args['profile']]['model'] and type(p.get('ctx_size')) is int and p['ctx_size'] > 0 and type(p.get('port')) is int and p['port'] > 0 for p in status.get('profiles', [])):
                error = {'status': 409, 'kind': 'model_not_ready', 'message': 'Requested local profile is not ready'}
            elif mode in ('busy', 'nocap', 'fail'):
                error = {'status': 502 if mode == 'fail' else 503, 'message': 'Owner refused local preparation'}
            else:
                pid = args['profile']
                loaded = next((p for p in status.get('profiles', []) if p.get('id') == pid and p.get('state') == 'ready'), None)
                if loaded is None:
                    loaded = {'id': pid, 'model': rows[pid]['model'], 'ctx_size': rows[pid]['ctx_size'], 'state': 'ready', 'port': 11499}
                    status.setdefault('profiles', []).append(loaded)
                    Path(state).write_text(json.dumps(status))
                    with Path(str(state)+'.starts').open('a') as f: f.write(pid+'\n')
                result = {'port': loaded['port']}
        elif op == 'shared.turn':
            result = await rig.shared_kv('turn', **args)
        elif op == 'manager.status':
            result = {'gpus': [{'index': 0, 'name': 'fixture', 'total_mb': 24000, 'used_mb': 0}], 'profiles': [], 'leases': [], 'claims': []}
        elif op == 'manager.chat_availability':
            result = {'busy': False, 'reason': '', 'ready_profiles': []}
        elif op == 'manager.http':
            result = {'status': 201 if args['method'] == 'POST' else 200, 'body': {'owner_response': True, 'path': args['path'], 'body': args['body']}}
        elif op == 'manager.kill':
            result = {'killed': args['args'][0]}
        else:
            error = {'status': 400, 'message': 'Unexpected fixture operation: '+op}
        return httpx.Response(200, json={'schema': envelope['schema'], 'request_id': envelope['request_id'],
                                        'ok': error is None, **({'error': error} if error else {'result': result})})
    owner = OrchestrationClient('http://fixture', 'fixture', transport=httpx.MockTransport(respond))
    owner.calls = calls
    return owner
