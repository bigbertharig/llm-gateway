"""Exercise the production startup path with both supported configurations."""
import pytest
from llm_gateway import __main__ as entrypoint

@pytest.mark.parametrize("mode", ["cloud", "integrated"])
def test_main_uses_current_settings(monkeypatch, tmp_path, caplog, mode):
    monkeypatch.setenv("GATEWAY_MODE", mode)
    monkeypatch.setenv("GATEWAY_BIND", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_API_KEYS_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("ORCHESTRATION_URL", "http://127.0.0.1:8092")
    monkeypatch.setenv("ORCHESTRATION_CONTROL_TOKEN", "startup-test-secret")
    seen = {}
    sentinel = object()
    def create(settings):
        seen["settings"] = settings
        return sentinel
    def serve(app, **kwargs):
        seen["app"] = app
        seen["kwargs"] = kwargs
    monkeypatch.setattr(entrypoint, "create_app", create)
    monkeypatch.setattr(entrypoint.uvicorn, "run", serve)
    with caplog.at_level("INFO"):
        entrypoint.main()
    assert seen["app"] is sentinel
    assert seen["settings"].runtime_enabled == (mode == "integrated")
    assert seen["kwargs"]["host"] == "127.0.0.1"
    assert seen["kwargs"]["timeout_graceful_shutdown"] == 10
    assert "startup-test-secret" not in caplog.text
