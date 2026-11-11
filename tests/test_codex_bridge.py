"""Codex app-server request compatibility without an account or GPU."""

import asyncio

from llm_gateway.dashboard.codex_bridge import CodexBridge


def test_existing_codex_thread_resumes_without_experimental_capability(tmp_path):
    bridge = CodexBridge()
    bridge.workspaces = [tmp_path]
    calls = []

    async def account():
        return {"connected": True}

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
    thread_id, answer = asyncio.run(bridge.turn("saved-thread", "Find the dashboard", str(tmp_path)))

    assert (thread_id, answer) == ("saved-thread", "Found it.")
    assert calls[0] == ("thread/resume", {"threadId": "saved-thread"})
    assert calls[1][0] == "turn/start"
