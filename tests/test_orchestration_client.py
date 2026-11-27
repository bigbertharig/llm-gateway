import asyncio
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from llm_gateway.app import create_app
from llm_gateway.orchestration_client import OrchestrationClient, ManagerError
from llm_gateway.settings import Settings
from runtime_fixture import fake_owner

def test_version_and_identity_mismatch_refused_without_fallback():
    for patch in ({'schema': 'orchestration-control/2'}, {'request_id': 'wrong'}):
        def handler(request):
            e = json.loads(request.content)
            return httpx.Response(200, json={'schema': e['schema'], 'request_id': e['request_id'], 'ok': True, 'result': {}, **patch})
        client = OrchestrationClient('http://owner', 'fixture', transport=httpx.MockTransport(handler))
        with pytest.raises(ManagerError, match='identity mismatch'):
            asyncio.run(client.call('rig.status'))

def test_uncertain_write_retains_request_identity_and_is_not_retried():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        raise httpx.ReadTimeout('lost response')
    client = OrchestrationClient('http://owner', 'fixture', transport=httpx.MockTransport(handler))
    with pytest.raises(ManagerError) as caught:
        asyncio.run(client.call('manager.load_model', {'args': ['x']}, request_id='original-id'))
    assert caught.value.extra['request_id'] == 'original-id'
    assert len(calls) == 1

def test_model_load_wait_extends_gateway_control_timeout():
    client = OrchestrationClient("http://owner", "fixture", timeout=10)
    calls = []
    async def capture(operation, arguments=None, **kwargs):
        calls.append((operation, arguments, kwargs))
        return {}
    client.call = capture
    path = "/v1/manager/models/load"
    asyncio.run(client.http("POST", path, {"model": "demo", "wait_s": 7200}))
    assert calls[-1][0] == "manager.http"
    assert calls[-1][2]["timeout"] == 7210
    asyncio.run(client.http("POST", path, {"model": "demo", "wait_s": -1}))
    assert isinstance(calls[-1][2]["timeout"], httpx.Timeout)
    assert calls[-1][2]["timeout"].read is None
    asyncio.run(client.http("GET", "/v1/manager/status"))
    assert calls[-1][2]["timeout"] is None
    asyncio.run(client.load_model("demo", wait_s=120))
    assert calls[-1][2]["timeout"] == 130

def test_manager_api_forwards_intent_status_and_request_identity():
    owner = fake_owner()
    with TestClient(create_app(Settings.from_env(), orchestration=owner)) as client:
        response = client.post('/v1/manager/models/load', headers={'X-Request-ID': 'operator-one'},
                               json={'model': 'local', 'cards': 2})
        assert response.status_code == 201
        assert response.json()['body'] == {'model': 'local', 'cards': 2}
    request = next(c for c in owner.calls if c['operation'] == 'manager.http')
    assert request['request_id'] == 'operator-one'
    assert request['arguments']['body'] == {'model': 'local', 'cards': 2}

def test_manager_api_accepts_bodyless_post_for_unload():
    owner = fake_owner()
    with TestClient(create_app(Settings.from_env(), orchestration=owner)) as client:
        response = client.post('/v1/manager/profiles/demo/unload')
        assert response.status_code == 201
    request = next(c for c in owner.calls if c['operation'] == 'manager.http')
    assert request['arguments']['body'] == {}

def test_gateway_has_no_local_decision_engine_or_command_fallback():
    root = Path(__file__).resolve().parents[1]
    assert not (root / 'src/llm_gateway/manager/core.py').exists()
    assert not (root / 'tools/generate_profiles.py').exists()
    client = (root / 'src/llm_gateway/orchestration_client.py').read_text()
    assert 'subprocess' not in client and 'create_subprocess' not in client
    with TestClient(create_app(Settings.from_env())) as app:
        assert app.get('/v1/manager/status').status_code == 503
        assert app.get('/health').status_code == 200
