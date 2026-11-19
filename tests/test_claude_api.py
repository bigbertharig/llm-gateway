"""Claude API provider uses private API keys and the shared harness tool runtime."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.dashboard import claude_api
from llm_gateway.dashboard import chat


class FakeResponse:
    status_code = 200
    def __init__(self, body):
        self.body = body
    def json(self):
        return self.body


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None
    async def post(self, url, *, json, headers):
        self.requests.append((url, json, headers))
        return FakeResponse(next(self.responses))


def _configured(monkeypatch, tmp_path):
    key = tmp_path / "api_key"
    key.write_text("test-key\n")
    key.chmod(0o600)
    monkeypatch.setenv("CLAUDE_API_KEY_FILE", str(key))
    monkeypatch.setenv("CLAUDE_API_MODEL", "claude-test")


def test_claude_requires_private_key(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path)
    api = claude_api.ClaudeApi()
    assert api.available()
    (tmp_path / "api_key").chmod(0o644)
    assert not api.available()


def test_claude_turn_uses_harness_tools(monkeypatch, tmp_path):
    _configured(monkeypatch, tmp_path)
    schema = {"type": "function", "function": {"name": "get_rig_status",
              "description": "Read rig status", "parameters": {"type": "object", "properties": {}}}}
    dispatch = {"get_rig_status": ([], {})}
    monkeypatch.setattr(claude_api.tool_runtime, "available", lambda *args: ([schema], dispatch))
    monkeypatch.setattr(claude_api.tool_runtime, "call", lambda *args: {"structuredContent": {"ok": True}})
    client = FakeClient([
        {"content": [{"type": "tool_use", "id": "tool-1", "name": "get_rig_status", "input": {}}]},
        {"content": [{"type": "text", "text": "Rig is ready."}], "usage": {"input_tokens": 20}},
    ])
    monkeypatch.setattr(claude_api.httpx, "AsyncClient", lambda **kwargs: client)
    result = asyncio.run(claude_api.ClaudeApi().turn([], "What is the rig status?",
                                                      mode="chat", workspace=None))
    assert result["answer"] == "Rig is ready."
    assert result["activity"][0]["tool"] == "get_rig_status"
    assert client.requests[0][1]["tools"][0]["name"] == "get_rig_status"
    assert client.requests[1][1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_claude_dashboard_conversation_does_not_load_rig(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "chat"))
    monkeypatch.setattr(chat.ClaudeApi, "available", lambda self: True)
    async def answer(self, messages, prompt, **kwargs):
        return {"answer": "Hello from Claude", "activity": [], "usage": {"input_tokens": 10,
                "output_tokens": 4}, "model": "claude-test"}
    monkeypatch.setattr(chat.ClaudeApi, "turn", answer)
    class Profiles:
        def all(self):
            return {}
    class Manager:
        pass
    app = FastAPI()
    app.include_router(chat.router(Manager(), Profiles(), None))
    with TestClient(app) as client:
        created = client.post("/dashboard/api/chat/conversations", json={"model": "Claude"})
        assert created.status_code == 201
        conversation = created.json()
        assert conversation["provider"] == "claude"
        cid = conversation["id"]
        prepared = client.post(f"/dashboard/api/chat/conversations/{cid}/prepare")
        assert prepared.json()["state"] == "ready"
        response = client.post(f"/dashboard/api/chat/conversations/{cid}/turn",
                               json={"model": "Claude", "message": "Hi"})
        assert response.status_code == 200, response.text
        saved = client.get(f"/dashboard/api/chat/conversations/{cid}").json()
        assert saved["messages"][-1]["content"] == "Hello from Claude"
