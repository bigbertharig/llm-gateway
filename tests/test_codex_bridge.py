"""Codex app-server request compatibility without an account or GPU."""

import asyncio

from llm_gateway.dashboard.codex_bridge import CodexBridge


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
                                                images=[image], model="test-codex"))

    assert (thread_id, answer) == ("saved-thread", "Found it.")
    assert calls[0] == ("thread/resume", {"threadId": "saved-thread"})
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["model"] == "test-codex"
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


def test_agent_status_includes_recent_activity_with_idle_state():
    bridge = CodexBridge()
    bridge.recent["saved-thread"] = [{"method": "item/started", "params": {
        "item": {"type": "commandExecution", "command": "old command"}},
        "_received_at": "2026-11-11T11:47:00+00:00"}]
    assert bridge.status("saved-thread") == {"events": [{
        "method": "item/started", "item": {"type": "commandExecution", "command": "old command"},
        "turn": None, "received_at": "2026-11-11T11:47:00+00:00",
    }], "approvals": [], "active": False}
