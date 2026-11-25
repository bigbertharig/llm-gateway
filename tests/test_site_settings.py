import pytest
from llm_gateway.settings import Settings

def test_cloud_is_default_without_orchestration(monkeypatch):
    monkeypatch.delenv('ORCHESTRATION_URL', raising=False)
    monkeypatch.delenv('GATEWAY_MODE', raising=False)
    monkeypatch.setenv('RIG_COMMAND', 'must never execute')
    settings = Settings.from_env()
    assert not settings.runtime_enabled
    assert not hasattr(settings, 'rig_command')

def test_local_models_require_explicit_owner_connection(monkeypatch):
    monkeypatch.setenv('GATEWAY_MODE', 'integrated')
    monkeypatch.delenv('ORCHESTRATION_URL', raising=False)
    with pytest.raises(RuntimeError, match='ORCHESTRATION_URL'):
        Settings.from_env().validate()
