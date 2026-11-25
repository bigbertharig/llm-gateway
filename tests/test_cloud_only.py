from dataclasses import replace
from fastapi.testclient import TestClient
from llm_gateway.app import create_app
from llm_gateway.settings import Settings


def test_cloud_dashboard_without_runtime_profiles_or_commands(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CHAT_DATA_ROOT', str(tmp_path / 'chat'))
    monkeypatch.setenv('GATEWAY_MODE', 'cloud')
    settings = replace(Settings.from_env(), profiles_path=tmp_path / 'absent.json',
                       rig_command=(), api_keys=('fixture',))
    with TestClient(create_app(settings)) as client:
        assert client.get('/v1/capabilities').status_code == 401
        client.headers['Authorization'] = 'Bearer fixture'
        page = client.get('/dashboard')
        assert page.status_code == 200
        assert 'data-runtime-enabled="false"' in page.text
        assert client.get('/v1/capabilities').json()['runtime'] is False
        assert client.get('/v1/models').json()['data'] == []
        options = client.get('/dashboard/api/chat/options')
        assert options.status_code == 200, options.text
        assert client.get('/v1/rig/status').status_code == 503
        assert client.post('/v1/operations/restart', json={}).status_code == 503
