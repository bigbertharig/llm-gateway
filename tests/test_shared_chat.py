"""Ready-only shared 35B chat routes and manager-owned child lifecycle."""
import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse

from llm_gateway.app import create_app
from runtime_fixture import fake_owner
from llm_gateway.orchestration_client import ManagerError
from llm_gateway.settings import Settings


class SharedRig:
    def __init__(self, ready=True):
        self.ready = ready
        self.chats = [{"chat_id": "main", "gpu": 0, "context_cap": 262144, "used_tokens": 0}]
        self.calls = []

    async def status(self):
        return {"profiles": [], "shared_kv_service": ({"status": "ready",
                "model": "qwen3.6:35b-a3b-xl", "port": 11524, "mode": "gpu0-main-q4",
                "instance_id": "qwen-load-1",
                "chats": [dict(row) for row in self.chats]} if self.ready else None)}

    async def shared_kv(self, action, *, model=None, chat_id=None, message=None, max_tokens=None,
                        context_tokens=None):
        self.calls.append((action, chat_id, message, max_tokens))
        if action == "start":
            self.ready = True
            return {"status": "ready"}
        if action == "attach":
            capacity = context_tokens or 262144
            reservation = {"reservation_id": "reservation-" + chat_id,
                           "service_instance_id": "qwen-load-1",
                           "context_capacity": capacity, "occupied_tokens": 0,
                           "placement": {"gpus": [1]}, "reserved_mib": 1537,
                           "reservation_scope": "gpu_exclusive", "claim": "worker"}
            row = {"chat_id": chat_id, "gpu": 1,
                   "context_cap": capacity, "used_tokens": 0,
                   "reservation": reservation}
            self.chats.append(row)
            return {**row, "service_instance_id": "qwen-load-1"}
        if action == "detach":
            row = next(row for row in self.chats if row["chat_id"] == chat_id)
            self.chats = [row for row in self.chats if row["chat_id"] != chat_id]
            return {"detached": chat_id, "released_reservation": row.get("reservation")}
        if action == "turn":
            row = next(row for row in self.chats if row["chat_id"] == chat_id)
            row["used_tokens"] += 32
            return {"content": json.dumps({"name": "delegate_task", "arguments": {
                    "agent_type": "coding", "task": "Compute 7 times 6"}}),
                    "generated_tokens": 20, "used_tokens": row["used_tokens"],
                    "finish_reason": "stop"}
        raise AssertionError(action)


def test_nemo_ready_route_uses_model_scoped_shared_service(tmp_path):
    class NemoRig(SharedRig):
        async def status(self):
            return {"profiles": [], "shared_kv_service": {"status": "ready",
                "model": "nemotron-3.5-bolt:74b-a9b", "instance_id": "load-1",
                "worker_capacity": {"status": "observed", "context_capacity": 262144,
                                    "free_gpu_count": 1 if len(self.chats) == 1 else 0,
                                    "free_gpu_ids": [1] if len(self.chats) == 1 else []},
                "chats": [dict(row) for row in self.chats]}}

        async def shared_kv(self, action, *, model=None, chat_id=None, message=None, max_tokens=None,
                            context_tokens=None):
            assert model == "nemotron-3.5-bolt:74b-a9b"
            assert action == "turn" and chat_id == "main"
            return {"content": "Nemo works", "generated_tokens": 2, "used_tokens": 6,
                    "finish_reason": "stop"}

    rig = NemoRig()
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
        route = "gpu-rig/shared-nemo-main"
        assert route in {row["id"] for row in client.get("/v1/rig/ready-roster").json()["data"]}
        response = client.post("/v1/chat/completions", json={"model": route,
            "messages": [{"role": "user", "content": "Hello"}], "_shared_instance_id": "load-1"})
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "Nemo works"
        stale = client.post("/v1/chat/completions", json={"model": route,
            "messages": [{"role": "user", "content": "Hello"}], "_shared_instance_id": "old-load"})
        assert stale.status_code == 409
















def settings(tmp_path):
    profiles = tmp_path / "profiles.json"
    profiles.write_text(json.dumps({'remote_profiles': [{'id': 'dummy', 'model': 'dummy:1b', 'ctx_size': 4096}]}))
    return Settings(upstream_host="127.0.0.1",
                    model_prefix="gpu-rig", bind="127.0.0.1", port=0,
                    start_timeout_seconds=30, request_timeout_seconds=30)


def test_shared_route_is_ready_and_translates_tool_call(tmp_path):
    rig = SharedRig()
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
        route = "gpu-rig/shared-35b-main"
        roster = client.get("/v1/rig/ready-roster")
        assert roster.status_code == 200, roster.text
        assert route in {row["id"] for row in roster.json()["data"]}
        response = client.post("/v1/chat/completions", json={"model": route,
            "messages": [{"role": "system", "content":
                          "Be concise\n<cwd>\n/private/receipt/workspace\n</cwd>"},
                         {"role": "user", "content": "Ask a child"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                      "description": "delegate", "parameters": {"type": "object"}}}],
            "max_tokens": 96})
        assert response.status_code == 200, response.text
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "delegate_task"
        assert "Be concise" in rig.calls[0][2]
        assert "<cwd>" not in rig.calls[0][2]
        assert "/private/receipt/workspace" not in rig.calls[0][2]
        assert rig.calls[0][3] == 96
        streamed = client.post("/v1/chat/completions", json={"model": route, "stream": True,
            "messages": [{"role": "user", "content": "Ask a child"}], "max_tokens": 96,
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                      "description": "delegate", "parameters": {"type": "object"}}}]})
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


def test_shared_route_translates_tool_call_after_tool_result(tmp_path):
    rig = SharedRig()
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
        response = client.post("/v1/chat/completions", json={
            "model": "gpu-rig/shared-35b-main",
            "messages": [{"role": "user", "content": "Delegate this"},
                         {"role": "assistant", "content": None, "tool_calls": [{
                             "id": "call-list", "type": "function", "function": {
                                 "name": "list_local_agents", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": "call-list", "content": "candidate found"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                       "description": "delegate", "parameters": {"type": "object"}}}],
            "max_tokens": 96})
        assert response.status_code == 200, response.text
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "delegate_task"


def test_shared_route_repairs_bare_tool_json_when_tools_are_unavailable(tmp_path):
    class Rig(SharedRig):
        async def shared_kv(self, action, *, model=None, chat_id=None, message=None, max_tokens=None,
                            context_tokens=None):
            if action == "turn" and "no tools are available" in (message or ""):
                self.calls.append((action, chat_id, message, max_tokens))
                return {"content": "A 44 by 44 inch poster should emphasize the research question and key result.",
                        "generated_tokens": 18, "used_tokens": 54, "finish_reason": "stop"}
            return await super().shared_kv(action, model=model, chat_id=chat_id,
                                           message=message, max_tokens=max_tokens)
    rig = Rig()
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
        response = client.post("/v1/chat/completions", json={"model": "gpu-rig/shared-35b-main",
            "messages": [{"role": "user", "content": "Help with a poster"}], "max_tokens": 96})
        assert response.status_code == 200, response.text
        answer = response.json()["choices"][0]["message"]["content"]
        assert "poster should emphasize" in answer
        assert len(rig.calls) == 2










def test_fenced_tool_request_gets_one_strict_repair(tmp_path):
    class RepairRig(SharedRig):
        async def shared_kv(self, action, **kwargs):
            result = await super().shared_kv(action, **kwargs)
            if action == "turn":
                prefix = 'I will use the tool.\n' if len([call for call in self.calls if call[0] == "turn"]) == 1 else ''
                result["content"] = prefix + '```json\n' + result["content"] + '\n```'
            return result
    rig = RepairRig()
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
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
    with TestClient(create_app(settings(tmp_path), orchestration=fake_owner(rig=rig))) as client:
        response = client.post("/v1/chat/completions", json={"model": "gpu-rig/shared-35b-main",
            "messages": [{"role": "user", "content": "Delegate"}],
            "tools": [{"type": "function", "function": {"name": "delegate_task",
                       "parameters": {"type": "object"}}}]})
        assert response.status_code == 200
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
