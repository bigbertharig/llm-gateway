"""Codex app-server request compatibility without an account or GPU."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.dashboard.codex_bridge import CodexBridge
from llm_gateway.dashboard.chat import ChatStore, router
from llm_gateway.manager.errors import ManagerError


def test_existing_codex_thread_resumes_without_experimental_capability(tmp_path):
    bridge = CodexBridge()
    bridge.workspaces = [tmp_path]
    calls = []

    async def account():
        raise AssertionError("turn must not block on account/read")

    async def call(method, params):
        calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "saved-thread"}}
        assert method == "turn/start"
        bridge.events["saved-thread"].put_nowait({"method": "item/completed", "params": {
            "turnId": "next-turn", "item": {"type": "agentMessage", "phase": "final_answer", "text": "Found it."}}})
        bridge.events["saved-thread"].put_nowait({"method": "turn/completed", "params": {
            "turnId": "next-turn", "turn": {"id": "next-turn", "status": "completed"}}})
        return {"turn": {"id": "next-turn"}}

    bridge.account = account
    bridge._call = call
    image = {"name": "screen.png", "type": "image/png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
    thread_id, answer = asyncio.run(bridge.turn("saved-thread", "Find the dashboard", str(tmp_path),
                                                images=[image], model="test-codex",
                                                effort="high", reviewer="auto_review"))

    assert (thread_id, answer) == ("saved-thread", "Found it.")
    assert calls[0] == ("thread/resume", {"threadId": "saved-thread",
                                          "config": {"approvals_reviewer": "auto_review"}})
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["model"] == "test-codex"
    assert calls[1][1]["effort"] == "high"
    assert calls[1][1]["approvalPolicy"] == "on-request"
    assert calls[1][1]["sandboxPolicy"]["type"] == "workspaceWrite"
    assert calls[1][1]["input"] == [
        {"type": "text", "text": "Find the dashboard"},
        {"type": "image", "url": image["data_url"]},
    ]


def test_account_state_is_cached_after_first_read():
    bridge = CodexBridge()
    calls = []

    async def call(method, params):
        calls.append(method)
        return {"account": {"type": "chatgpt", "email": "operator@example.test"}}

    bridge._call = call

    async def check():
        assert (await bridge.account())["connected"]
        bridge.events["active-thread"] = asyncio.Queue()
        assert (await bridge.account())["email"] == "operator@example.test"

    asyncio.run(check())
    assert calls == ["account/read"]


def test_new_codex_thread_uses_selected_reviewer_without_expanding_sandbox(tmp_path):
    bridge = CodexBridge()
    bridge.workspaces = [tmp_path]
    calls = []

    async def call(method, params):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "new-thread"}}
        bridge.events["new-thread"].put_nowait({"method": "item/completed", "params": {
            "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "Done."}}})
        bridge.events["new-thread"].put_nowait({"method": "turn/completed", "params": {
            "turnId": "turn-1", "turn": {"id": "turn-1", "status": "completed"}}})
        return {"turn": {"id": "turn-1"}}

    bridge._call = call
    assert asyncio.run(bridge.turn(None, "Work", str(tmp_path), reviewer="auto_review")) == ("new-thread", "Done.")
    assert calls[0][1]["config"] == {"approvals_reviewer": "auto_review"}
    assert calls[0][1]["sandbox"] == "workspace-write"
    assert calls[1][1]["sandboxPolicy"]["networkAccess"] is False


def test_agent_status_includes_recent_activity_with_idle_state():
    bridge = CodexBridge()
    bridge.recent["saved-thread"] = [
        {"method": "item/started", "params": {
            "item": {"type": "commandExecution", "command": "old command"}},
            "_received_at": "2026-11-11T11:47:00+00:00"},
        {"method": "item/completed", "params": {
            "item": {"type": "agentMessage", "text": "already in the transcript"}}},
        {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
    ]
    assert bridge.status("saved-thread") == {"events": [{
        "method": "item/started", "item": {"type": "commandExecution", "command": "old command"},
        "turn": None, "received_at": "2026-11-11T11:47:00+00:00",
    }], "approvals": [], "active": False}


def test_codex_preferences_and_activity_survive_store_reopen(tmp_path):
    store = ChatStore(tmp_path)
    conversation = store.create("Codex", "codex", str(tmp_path))
    cid = conversation["id"]
    store.set_codex_settings(cid, "gpt-test", "medium", "auto_review")
    turn_id = store.begin_turn(cid, 0, "Codex", "Fix it")
    activity = [{"method": "item/completed", "item": {"id": "step-1", "type": "commandExecution",
                                                      "command": "pytest"}}]
    store.append_exchange(cid, 0, "Codex", "Fix it", "Done.", turn_id=turn_id, activity=activity)
    restored = ChatStore(tmp_path).get(cid)
    assert (restored["codex_model"], restored["codex_effort"], restored["codex_reviewer"]) == (
        "gpt-test", "medium", "auto_review")
    assert restored["messages"][1]["activity"] == activity


def test_dashboard_codex_model_effort_and_reviewer_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "chat"))
    bridge = CodexBridge()
    bridge.workspaces = [tmp_path]

    async def account():
        return {"connected": True}

    async def models():
        return [{"model": "gpt-test", "displayName": "GPT Test", "isDefault": True,
                 "supportedReasoningEfforts": [{"reasoningEffort": "medium", "description": "Balanced"}]}]

    bridge.account = account
    bridge.models = models
    app = FastAPI()
    app.include_router(router(None, None, None, codex=bridge))
    client = TestClient(app)
    assert client.get("/dashboard/api/chat/codex/models").json()["models"][0]["model"] == "gpt-test"
    conversation = client.post("/dashboard/api/chat/conversations", json={
        "model": "Codex", "cwd": str(tmp_path)}).json()
    path = f"/dashboard/api/chat/conversations/{conversation['id']}/codex/settings"
    with pytest.raises(ManagerError, match="Reasoning effort is not available"):
        client.post(path, json={"model": "gpt-test", "effort": "high", "reviewer": "auto_review"})
    response = client.post(path, json={"model": "gpt-test", "effort": "medium", "reviewer": "auto_review"})
    assert response.status_code == 200, response.text
    saved = client.get(f"/dashboard/api/chat/conversations/{conversation['id']}").json()
    assert (saved["codex_model"], saved["codex_effort"], saved["codex_reviewer"]) == (
        "gpt-test", "medium", "auto_review")
