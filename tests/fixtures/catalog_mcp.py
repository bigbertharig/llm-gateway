"""Deterministic imported-catalog transport fixture, not a product tool implementation."""
import json
from pathlib import Path
import sys
catalog = json.loads((Path(__file__).parents[2] / 'src/llm_gateway/dashboard/tool_catalog.json').read_text())
name = 'github_public' if 'github' in Path(__file__).name else 'workspace_git'
# The configured filename identifies which imported interface this fixture serves.
if '--server' in sys.argv:
    name = sys.argv[sys.argv.index('--server') + 1]
for line in sys.stdin:
    packet = json.loads(line)
    if 'id' not in packet: continue
    method = packet['method']
    if method == 'initialize':
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': name, 'version': 'fixture'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': row['name'], 'description': row.get('description', ''), 'inputSchema': row.get('input_schema', row.get('inputSchema', {'type': 'object'}))} for row in catalog['servers'][name]['tools']]}
    elif method == 'tools/call':
        result = {'content': [{'type': 'text', 'text': '{"output":""}'}], 'structuredContent': {'output': ''}}
    else: result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': packet['id'], 'result': result}), flush=True)
