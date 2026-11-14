"""Ready-only shared 35B chat routes and manager-owned child lifecycle."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.manager.routes import router as manager_router
from llm_gateway.settings import Settings


class SharedRig:
    def __init__(self):
        self.chats = [{"chat_id": "main", "gpu": 0, "context_cap": 262144, "used_tokens": 0}]
        self.calls = []

    async def status(self):
        return {"profiles": [], "shared_kv_service": {"status": "ready",
                "model": "qwen3.6:35b-a3b-xl", "port": 11524, "mode": "gpu0-main-q4",
                "chats": [dict(row) for row in self.chats]}}

    async def shared_kv(self, action, *, chat_id=None, message=None, max_tokens=None):
        self.calls.append((action, chat_id, message, max_tokens))
        if action == "attach":
            row = {"chat_id": chat_id, "gpu": 1, "context_cap": 262144, "used_tokens": 0}
            self.chats.append(row)
            return row
        if action == "detach":
            self.chats = [row for row in self.chats if row["chat_id"] != chat_id]
            return {"detached": chat_id}
        if action == "turn":
            row = next(row for row in self.chats if row["chat_id"] == chat_id)
            row["used_tokens"] += 32
            return {"content": json.dumps({"name": "delegate_task", "arguments": {
                    "agent_type": "coding", "task": "Compute 7 times 6"}}),
                    "generated_tokens": 20, "used_tokens": row["used_tokens"],
                    "finish_reason": "stop"}
        raise AssertionError(action)

    def loading(self):
        return []


def settings(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({'remote_profiles': [{'id': 'dummy', 'model': 'dummy:1b', 'ctx_size': 4096}]}))
    return Settings(rig_command=("rig",), profiles_path=profiles, upstream_host="127.0.0.1",
                    model_prefix="gpu-rig", bind="127.0.0.1", port=0,
                    start_timeout_seconds=30, request_timeout_seconds=30)


def test_shared_route_is_ready_and_translates_tool_call(tmp_path):
    rig = SharedRig()
    with TestClient(create_app(settings(tmp_path), rig=rig)) as client:
        route = "gpu-rig/shared-35b-main"
        roster = client.get("/v1/rig/ready-roster")
        assert roster.status_code == 200, roster.text
        assert route in {row["id"] for row in roster.json()["data"]}
        response = client.post("/v1/chat/completions", json={"model": route,
            "messages": [{"role": "system", "content": "Be concise"},
                         {"role": "user", "content": "Ask a child"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                      "description": "delegate", "parameters": {"type": "object"}}}],
            "max_tokens": 96})
        assert response.status_code == 200, response.text
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "delegate_task"
        assert "Be concise" in rig.calls[0][2]
        assert rig.calls[0][3] == 96
        streamed = client.post("/v1/chat/completions", json={"model": route, "stream": True,
            "messages": [{"role": "user", "content": "Ask a child"}], "max_tokens": 96})
        assert streamed.status_code == 200, streamed.text
        assert "data: [DONE]" in streamed.text
        assert '"object":"chat.completion.chunk"' in streamed.text
        responses = client.post("/v1/responses", json={"model": route, "stream": True,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Ask a child"}]}],
            "tools": [{"type": "function", "name": "delegate_task", "parameters": {"type": "object"}}]})
        assert responses.status_code == 200, responses.text
        assert "response.function_call_arguments.done" in responses.text
        assert "response.completed" in responses.text
        assert client.post("/v1/chat/completions", json={"model": "gpu-rig/shared-35b-missing",
                   "messages": [{"role": "user", "content": "hi"}]}).status_code == 409


def test_manager_attaches_and_releases_cataloged_child(monkeypatch):
    rig = SharedRig()
    class Manager:
        pass
    manager = Manager()
    manager.rig = rig
    monkeypatch.setattr("llm_gateway.manager.routes.models.catalog", lambda: {
        "subagent_catalog": {"schema_version": 1, "candidates": {
            "qwen35_shared_1060": {"provider": "rig_shared_chat",
                "model": "qwen3.6:35b-a3b-xl", "agent_types": ["coding"]}}}})
    app = FastAPI()
    app.include_router(manager_router(manager, lambda: {}, "gpu-rig"))
    with TestClient(app) as client:
        got = client.post("/v1/manager/agent-routes/resolve", json={
            "parent_route": "gpu-rig/shared-35b-main", "agent_type": "coding",
            "candidate_id": "qwen35_shared_1060"})
        assert got.status_code == 200, got.text
        child = got.json()
        assert child["ephemeral"] is True and child["gpu_ids"] == [1]
        assert child["route"].startswith("gpu-rig/shared-35b-child-")
        released = client.post("/v1/manager/agent-routes/release", json={
            "parent_route": "gpu-rig/shared-35b-main", "route": child["route"]})
        assert released.status_code == 200, released.text
        assert len(rig.chats) == 1


def test_fenced_tool_request_gets_one_strict_repair(tmp_path):
    class RepairRig(SharedRig):
        async def shared_kv(self, action, **kwargs):
            result = await super().shared_kv(action, **kwargs)
            if action == "turn":
                prefix = 'I will use the tool.\n' if len([call for call in self.calls if call[0] == "turn"]) == 1 else ''
                result["content"] = prefix + '```json\n' + result["content"] + '\n```'
            return result
    rig = RepairRig()
    with TestClient(create_app(settings(tmp_path), rig=rig)) as client:
        response = client.post("/v1/chat/completions", json={"model": "gpu-rig/shared-35b-main",
            "messages": [{"role": "user", "content": "Delegate this"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                      "parameters": {"type": "object"}}}]})
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
        assert len([call for call in rig.calls if call[0] == "turn"]) == 2


def test_single_call_wrapper_is_translated(tmp_path):
    class WrapperRig(SharedRig):
        async def shared_kv(self, action, **kwargs):
            result = await super().shared_kv(action, **kwargs)
            if action == "turn":
                inner = json.loads(result["content"])
                result["content"] = json.dumps({"name": "tool_call", "arguments": {"calls": [inner]}})
            return result
    rig = WrapperRig()
    with TestClient(create_app(settings(tmp_path), rig=rig)) as client:
        response = client.post("/v1/chat/completions", json={"model": "gpu-rig/shared-35b-main",
            "messages": [{"role": "user", "content": "Delegate"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                       "parameters": {"type": "object"}}}]})
        assert response.status_code == 200
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
