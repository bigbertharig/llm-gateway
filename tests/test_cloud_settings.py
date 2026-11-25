"""Cloud settings keep secrets private and expose curated provider models."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from llm_gateway.dashboard import chat, cloud, openrouter_api
from llm_gateway.dashboard.cloud_settings import CloudSettings
from llm_gateway.orchestration_client import ManagerError


def app_for(settings: CloudSettings) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(
        ManagerError, lambda _request, exc: JSONResponse(
            {"error": {"message": str(exc)}}, status_code=exc.status))
    app.include_router(cloud.router(settings))
    return app


def test_cloud_api_stores_keys_without_returning_them(tmp_path):
    settings = CloudSettings(tmp_path / "cloud")
    with TestClient(app_for(settings)) as client:
        saved = client.post("/dashboard/api/cloud/openrouter", json={
            "api_key": "or-secret", "models": ["qwen/qwen4-max", "google/gemini-4-neon"]})
        assert saved.status_code == 200
        body = client.get("/dashboard/api/cloud/settings").json()
    assert body["openrouter"] == {"api_key_configured": True,
                                  "models": ["qwen/qwen4-max", "google/gemini-4-neon"]}
    assert "or-secret" not in json.dumps(body)
    assert (tmp_path / "cloud/openrouter.key").stat().st_mode & 0o077 == 0


def test_claude_cloud_settings_override_environment(tmp_path, monkeypatch):
    settings = CloudSettings(tmp_path / "cloud")
    settings.set_secret("claude", "stored-key")
    settings.set_claude_model("claude-stored")
    monkeypatch.setenv("CLAUDE_API_MODEL", "claude-env")
    assert settings.claude_configuration() == ("stored-key", "claude-stored")


def test_openrouter_catalog_includes_prices_and_release_year(tmp_path, monkeypatch):
    settings = CloudSettings(tmp_path / "cloud")
    settings.set_secret("openrouter", "or-secret")

    class CatalogClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def get(self, url, *, headers):
            assert headers["Authorization"] == "Bearer or-secret"
            return FakeResponse({"data": [{"id": "example/model", "name": "Example",
                "context_length": 131072, "created": 1735689600,
                "pricing": {"prompt": "0.0000015", "completion": "0.000006"}}]})

    monkeypatch.setattr(cloud.httpx, "AsyncClient", lambda **kwargs: CatalogClient())
    with TestClient(app_for(settings)) as client:
        response = client.get("/dashboard/api/cloud/openrouter/models")
    assert response.status_code == 200
    assert response.json()["models"][0] == {
        "id": "example/model", "name": "Example", "context_length": 131072,
        "prompt_price_per_million": 1.5, "completion_price_per_million": 6.0,
        "release_year": 2025, "description": ""}


def test_codex_subscription_and_api_auth_are_independent(tmp_path):
    class Codex:
        def __init__(self, method=None): self.method = method
        async def account(self):
            return {"connected": self.method is not None, "auth_method": self.method}
        async def login_api_key(self, key):
            assert key == "sk-api-only"
            self.method = "apiKey"
            return await self.account()

    subscription, api = Codex("chatgpt"), Codex()
    app = FastAPI()
    app.add_exception_handler(
        ManagerError, lambda _request, exc: JSONResponse(
            {"error": {"message": str(exc)}}, status_code=exc.status))
    app.include_router(cloud.router(CloudSettings(tmp_path / "cloud"), subscription,
                                    codex_api=api))
    with TestClient(app) as client:
        saved = client.post("/dashboard/api/cloud/codex/api-key",
                            json={"api_key": "sk-api-only"})
        status = client.get("/dashboard/api/cloud/settings").json()
    assert saved.status_code == 200
    assert status["codex_subscription"]["auth_method"] == "chatgpt"
    assert status["codex_api"]["auth_method"] == "apiKey"
    assert "sk-api-only" not in json.dumps(status)


class FakeResponse:
    status_code = 200
    def __init__(self, body): self._body = body
    def json(self): return self._body


class FakeClient:
    def __init__(self, responses): self.responses = iter(responses); self.requests = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def post(self, url, *, json, headers):
        self.requests.append((url, json, headers))
        return FakeResponse(next(self.responses))


def test_openrouter_turn_uses_selected_model_and_tools(tmp_path, monkeypatch):
    settings = CloudSettings(tmp_path / "cloud")
    settings.set_secret("openrouter", "or-secret")
    settings.set_openrouter_models(["qwen/qwen4-max"])
    schema = {"type": "function", "function": {"name": "get_rig_status",
              "description": "Read status", "parameters": {"type": "object", "properties": {}}}}
    dispatch = {"get_rig_status": ([], {})}
    monkeypatch.setattr(openrouter_api.tool_runtime, "available", lambda *args: ([schema], dispatch))
    monkeypatch.setattr(openrouter_api.tool_runtime, "call",
                        lambda *args: {"structuredContent": {"ready": True}})
    client = FakeClient([
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1",
          "type": "function", "function": {"name": "get_rig_status", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "Rig is ready."}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
    ])
    monkeypatch.setattr(openrouter_api.httpx, "AsyncClient", lambda **kwargs: client)
    result = asyncio.run(openrouter_api.OpenRouterApi(settings).turn(
        "qwen/qwen4-max", [], "Status?", mode="chat", workspace=None))
    assert result["answer"] == "Rig is ready."
    assert client.requests[0][1]["model"] == "qwen/qwen4-max"
    assert client.requests[1][1]["messages"][-1]["role"] == "tool"
    assert "or-secret" not in json.dumps(result)


def test_openrouter_dashboard_conversation_uses_curated_model(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "chat"))
    settings = CloudSettings(tmp_path / "cloud")
    settings.set_secret("openrouter", "or-secret")
    settings.set_openrouter_models(["qwen/qwen4-max"])

    async def answer(self, model, messages, prompt, **kwargs):
        assert model == "qwen/qwen4-max"
        return {"answer": "Hello from Qwen4", "activity": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4}, "model": model}
    monkeypatch.setattr(chat.OpenRouterApi, "turn", answer)

    class Profiles:
        shared = {}
        @property
        def chat_offers(self):
            return self.all()
        def all(self): return {}
    class Manager: pass
    app = FastAPI()
    app.add_exception_handler(
        ManagerError, lambda _request, exc: JSONResponse(
            {"error": {"message": str(exc)}}, status_code=exc.status))
    app.include_router(chat.router(Manager(), Profiles(), None, cloud_settings=settings))
    with TestClient(app) as client:
        created = client.post("/dashboard/api/chat/conversations", json={
            "model": "openrouter/qwen/qwen4-max", "mode": "chat"})
        assert created.status_code == 201
        conversation = created.json()
        response = client.post(f"/dashboard/api/chat/conversations/{conversation['id']}/turn", json={
            "model": "openrouter/qwen/qwen4-max", "message": "Hi"})
        saved = client.get(f"/dashboard/api/chat/conversations/{conversation['id']}").json()
    assert conversation["provider"] == "openrouter"
    assert response.status_code == 200
    assert saved["messages"][-1]["content"] == "Hello from Qwen4"


def test_claude_code_subscription_conversation_persists_cli_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "chat"))
    monkeypatch.setenv("CODEX_CHAT_RESEARCH_ROOT", str(tmp_path / "research"))

    class ClaudeCode:
        calls = []
        async def account(self): return {"connected": True, "auth_method": "claude.ai"}
        async def turn(self, session_id, prompt, *, workspace, **kwargs):
            self.calls.append((session_id, prompt, workspace))
            return {"answer": "Subscription answer", "session_id": session_id or "session-1",
                    "usage": {"input_tokens": 6, "output_tokens": 2}, "model": "Claude Code"}

    class Profiles:
        shared = {}
        @property
        def chat_offers(self):
            return self.all()
        def all(self): return {}
    class Manager: pass
    bridge = ClaudeCode()
    app = FastAPI()
    app.add_exception_handler(
        ManagerError, lambda _request, exc: JSONResponse(
            {"error": {"message": str(exc)}}, status_code=exc.status))
    app.include_router(chat.router(Manager(), Profiles(), None, claude_code=bridge))
    with TestClient(app) as client:
        created = client.post("/dashboard/api/chat/conversations", json={
            "model": "Claude Code", "mode": "chat"}).json()
        first = client.post(f"/dashboard/api/chat/conversations/{created['id']}/turn", json={
            "model": "Claude Code", "message": "Hi"})
        second = client.post(f"/dashboard/api/chat/conversations/{created['id']}/turn", json={
            "model": "Claude Code", "message": "Continue"})
        saved = client.get(f"/dashboard/api/chat/conversations/{created['id']}").json()
    assert created["provider"] == "claude_code"
    assert first.status_code == second.status_code == 200
    assert bridge.calls[0][0] is None and bridge.calls[1][0] == "session-1"
    assert saved["thread_id"] == "session-1"
