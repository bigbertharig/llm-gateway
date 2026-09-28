import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from llm_gateway.main import create_app
from llm_gateway.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "openrouter_models": ["example/cloud-model"],
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        gateway_api_key="gateway-secret",
        openrouter_api_key="openrouter-secret",
        openrouter_base_url="https://openrouter.test/v1",
        orchestrator_url="http://rig.test:8790",
        orchestrator_api_key="orchestrator-secret",
        model_catalog_path=catalog,
        local_session_renew_seconds=3600,
    )


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer gateway-secret"}


def test_v1_requires_authentication(tmp_path: Path):
    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with TestClient(app) as client:
        response = client.get("/v1/models")
    assert response.status_code == 401


def test_models_combines_configured_cloud_and_orchestrator_models(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer orchestrator-secret"
        return httpx.Response(200, json={"models": [{"id": "qwen-coder"}]})

    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.get("/v1/models", headers=auth())

    assert response.status_code == 200
    assert response.headers["x-local-models-status"] == "available"
    assert [item["id"] for item in response.json()["data"]] == [
        "openrouter/example/cloud-model",
        "local/qwen-coder",
    ]


def test_openrouter_request_rewrites_model(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer openrouter-secret"
        body = json.loads(request.content)
        assert body["model"] == "example/cloud-model"
        return httpx.Response(200, json={"id": "cloud-result"})

    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "openrouter/example/cloud-model", "messages": []},
        )

    assert response.status_code == 200
    assert response.json() == {"id": "cloud-result"}


def test_local_request_acquires_forwards_and_releases(tmp_path: Path):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST" and request.url.path.endswith("/acquire"):
            return httpx.Response(
                200,
                json={
                    "endpoint": "http://worker.test:11435/v1",
                    "runtime_model": "runtime-qwen",
                    "session_id": "session-1",
                },
            )
        if request.method == "POST" and request.url.host == "worker.test":
            body = json.loads(request.content)
            assert body["model"] == "runtime-qwen"
            return httpx.Response(200, json={"id": "local-result"})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={"model": "local/qwen-coder", "messages": []},
        )

    assert response.status_code == 200
    assert response.json() == {"id": "local-result"}
    assert calls == [
        ("POST", "http://rig.test:8790/v1/local/models/qwen-coder/acquire"),
        ("POST", "http://worker.test:11435/v1/chat/completions"),
        ("DELETE", "http://rig.test:8790/v1/local/sessions/session-1"),
    ]


def test_streaming_preserves_upstream_error_status(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    app = create_app(make_settings(tmp_path), transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers=auth(),
            json={
                "model": "openrouter/example/cloud-model",
                "messages": [],
                "stream": True,
            },
        )

    assert response.status_code == 429
    assert response.json()["error"]["message"] == "rate limited"

