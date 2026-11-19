"""Codex app-server request compatibility without an account or GPU."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.dashboard.codex_bridge import CodexBridge, CodexBridgeError, _delegate_receipt
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
                                                effort="high", reviewer="auto_review", access="workspace"))

    assert (thread_id, answer) == ("saved-thread", "Found it.")
    assert calls[0][0] == "thread/resume"
    assert calls[0][1]["threadId"] == "saved-thread"
    resume_config = calls[0][1]["config"]
    assert resume_config["approvals_reviewer"] == "auto_review"
    assert resume_config["web_search"] == "disabled"
    assert resume_config["mcp_servers"]["workspace_git"]["enabled_tools"] == [
        "get_workspace_git_status", "get_workspace_git_log", "get_workspace_git_diff"]
    assert calls[1][0] == "turn/start"
    assert calls[1][1]["model"] == "test-codex"
    assert calls[1][1]["effort"] == "high"
    assert calls[1][1]["approvalPolicy"] == "on-request"
    assert calls[1][1]["permissions"] == ":workspace"
    assert calls[1][1]["input"] == [
        {"type": "text", "text": "Find the dashboard"},
        {"type": "image", "url": image["data_url"]},
    ]


def test_active_turn_can_be_steered_and_interrupted():
    bridge = CodexBridge()
    bridge.active_turns["thread-1"] = "turn-1"
    calls = []

    async def call(method, params):
        calls.append((method, params))
        return {}

    bridge._call = call
    assert asyncio.run(bridge.steer("thread-1", "Use the smaller change")) == "turn-1"
    assert asyncio.run(bridge.interrupt("thread-1")) == "turn-1"
    assert calls == [
        ("turn/steer", {"threadId": "thread-1", "expectedTurnId": "turn-1",
                        "input": [{"type": "text", "text": "Use the smaller change"}]}),
        ("turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}),
    ]
    bridge.active_turns.clear()
    with pytest.raises(CodexBridgeError, match="no longer has an active turn"):
        asyncio.run(bridge.interrupt("thread-1"))


def test_chat_codex_defaults_to_local_full_access_and_research_tools(tmp_path, monkeypatch):
    from llm_gateway.dashboard import dashboard_mcp_servers

    bridge = CodexBridge()
    bridge.research_root = tmp_path / "research"
    workspace = bridge.research_workspace()
    observed = []
    def tools(workspace=None, mode="coding", conversation_id=None):
        observed.append((workspace, mode))
        return {"mcp_servers": {"rig_search": {"command": "search"}}}
    monkeypatch.setattr(dashboard_mcp_servers, "cloud_config", tools)
    calls = []
    async def call(method, params):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "research-thread"}}
        bridge.events["research-thread"].put_nowait({"method": "item/completed", "params": {
            "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "Found it."}}})
        bridge.events["research-thread"].put_nowait({"method": "turn/completed", "params": {
            "turnId": "turn-1", "turn": {"id": "turn-1", "status": "completed"}}})
        return {"turn": {"id": "turn-1"}}
    bridge._call = call
    thread, answer = asyncio.run(bridge.turn(None, "Find it", str(workspace), mode="chat"))
    assert (thread, answer) == ("research-thread", "Found it.")
    assert observed == [(None, "chat")]
    assert calls[0][1]["permissions"] == ":danger-full-access"
    assert calls[0][1]["approvalPolicy"] == "never"
    assert calls[1][1]["permissions"] == ":danger-full-access"
    assert "sandboxPolicy" not in calls[1][1]
    assert calls[1][1]["approvalPolicy"] == "never"


def test_mobile_resume_preserves_saved_codex_access_and_reviewer(tmp_path, monkeypatch):
    from llm_gateway.dashboard import dashboard_mcp_servers

    bridge = CodexBridge()
    bridge.workspaces = [tmp_path]
    seen = []
    def tools(**kwargs):
        seen.append(kwargs)
        return {"mcp_servers": {}}
    monkeypatch.setattr(dashboard_mcp_servers, "cloud_config", tools)
    calls = []
    async def call(method, params):
        calls.append((method, params))
        if method == "thread/resume":
            return {"thread": {"id": "existing-thread"}}
        bridge.events["existing-thread"].put_nowait({"method": "item/completed", "params": {
            "turnId": "turn-1", "item": {"type": "agentMessage", "phase": "final_answer", "text": "Done."}}})
        bridge.events["existing-thread"].put_nowait({"method": "turn/completed", "params": {
            "turnId": "turn-1", "turn": {"id": "turn-1", "status": "completed"}}})
        return {"turn": {"id": "turn-1"}}
    bridge._call = call
    thread, answer = asyncio.run(bridge.turn("existing-thread", "Continue", str(tmp_path),
        mode="coding", access="local", reviewer="auto_review", surface="mobile"))
    assert (thread, answer) == ("existing-thread", "Done.")
    assert seen[0]["surface"] == "mobile"
    assert calls[0][1]["config"]["approvals_reviewer"] == "auto_review"
    assert calls[1][1]["permissions"] == ":danger-full-access"
    assert calls[1][1]["approvalPolicy"] == "never"


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
    assert calls == ["account/read", "account/rateLimits/read"]


def test_new_codex_thread_uses_local_full_access_without_sandbox_prompts(tmp_path):
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
    config = calls[0][1]["config"]
    assert config["approvals_reviewer"] == "auto_review"
    assert config["web_search"] == "disabled"
    assert config["mcp_servers"]["workspace_git"]["enabled_tools"] == [
        "get_workspace_git_status", "get_workspace_git_log", "get_workspace_git_diff"]
    assert calls[0][1]["permissions"] == ":danger-full-access"
    assert calls[1][1]["permissions"] == ":danger-full-access"


def test_codex_thread_registers_local_agent_mcp_when_configured(tmp_path, monkeypatch):
    script = tmp_path / "local_agent_mcp.py"
    script.write_text("# fixture\n")
    receipts = tmp_path / "receipts"
    monkeypatch.setenv("CODEX_LOCAL_AGENT_MCP_SCRIPT", str(script))
    monkeypatch.setenv("CODEX_LOCAL_AGENT_RECEIPTS", str(receipts))
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
    asyncio.run(bridge.turn(None, "Use local agents to help", str(tmp_path)))
    server = calls[0][1]["config"]["mcp_servers"]["local_agents"]
    assert server["args"] == [str(script), "--cloud-parent", "--receipt-dir", str(receipts),
                              "--workspace-root", str(tmp_path), "--coding-only"]
    assert server["enabled_tools"] == ["list_local_agents", "delegate_task"]
    assert "default_tools_approval_mode" not in server
    assert receipts.is_dir()
    assert calls[1][1]["input"][0]["text"] == "Use local agents to help"


def test_codex_search_mcp_uses_configured_router_and_cpu_reader(tmp_path, monkeypatch):
    script = tmp_path / "mcp_server.py"
    script.write_text("# fixture\n")
    env_file = tmp_path / "search.env"
    env_file.write_text("RIG_SEARCH_PROVIDERS=searxng,exa\n")
    monkeypatch.setenv("CODEX_SEARCH_MCP_SCRIPT", str(script))
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENV_FILE", str(env_file))
    bridge = CodexBridge()
    server = bridge.search_config()["mcp_servers"]["rig_search"]
    assert server["args"] == [str(script), "--env-file", str(env_file),
                              "--endpoint", "http://127.0.0.1:8888"]
    assert server["enabled_tools"] == ["search_web", "fetch_page"]
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENDPOINT", "https://example.com/search")
    with pytest.raises(CodexBridgeError, match="loopback"):
        bridge.search_config()


def test_saved_codex_conversation_gets_narrow_reset_tool(tmp_path, monkeypatch):
    from llm_gateway.dashboard import dashboard_mcp_servers
    script = tmp_path / "gateway_reset_mcp.py"
    script.write_text("# fixture\n")
    history = tmp_path / "chat"
    history.mkdir()
    monkeypatch.setenv("CODEX_GATEWAY_RESET_MCP_SCRIPT", str(script))
    monkeypatch.setenv("CHAT_DATA_ROOT", str(history))
    config = dashboard_mcp_servers.cloud_config(
        workspace=tmp_path, mode="coding", conversation_id="abc123")
    server = config["mcp_servers"]["gateway_reset"]
    assert server["args"] == [str(script), "--conversation-id", "abc123",
                              "--data-root", str(history)]
    assert server["enabled_tools"] == ["restart_gateway_and_resume"]


def test_search_elicitation_is_allowlisted():
    bridge = CodexBridge()
    writes = []

    async def write(message):
        writes.append(message)

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        bridge.events["thread-1"] = asyncio.Queue()
        bridge._write = write
        reader.feed_data((json.dumps({"id": 9, "method": "mcpServer/elicitation/request", "params": {
            "threadId": "thread-1", "serverName": "rig_search", "mode": "form",
            "message": 'Allow the rig_search MCP server to run tool "search_web"?',
            "requestedSchema": {"type": "object", "properties": {}}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()
        assert bridge.status("thread-1")["approvals"] == []

    asyncio.run(check())
    assert writes == [{"id": 9, "result": {"action": "accept", "content": {}}}]


@pytest.mark.parametrize("tool_name", ["list_local_agents", "delegate_task"])
def test_allowlisted_local_agent_elicitation_is_accepted_once(tool_name):
    bridge = CodexBridge()
    writes = []

    async def write(message):
        writes.append(message)

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        bridge.events["thread-1"] = asyncio.Queue()
        bridge._write = write
        reader.feed_data((json.dumps({"id": 7, "method": "mcpServer/elicitation/request", "params": {
            "threadId": "thread-1", "serverName": "local_agents", "mode": "form",
            "message": f'Allow the local_agents MCP server to run tool "{tool_name}"?',
            "requestedSchema": {"type": "object", "properties": {}}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()
        assert bridge.status("thread-1")["approvals"] == []

    asyncio.run(check())
    assert writes == [{"id": 7, "result": {"action": "accept", "content": {}}}]


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
    }], "approvals": [], "active": False, "context": None,
        "context_pending": False, "rate_limits": None}


def test_progress_messages_are_exposed_but_final_answer_is_not_duplicated():
    bridge = CodexBridge()
    bridge.recent["thread-1"] = [
        {"method": "item/completed", "params": {"item": {
            "id": "update-1", "type": "agentMessage", "phase": "commentary",
            "text": "I found the relevant path."}}},
        {"method": "item/completed", "params": {"item": {
            "id": "answer-1", "type": "agentMessage", "phase": "final_answer",
            "text": "Done."}}},
    ]
    events = bridge.status("thread-1")["events"]
    assert [event["item"]["text"] for event in events] == ["I found the relevant path."]


def test_progress_messages_are_not_cut_at_the_command_preview_limit():
    bridge = CodexBridge()
    commentary = "Measured placement update: " + "context " * 140
    assert len(commentary) > 800
    bridge.recent["thread-1"] = [{"method": "item/completed", "params": {"item": {
        "id": "update-long", "type": "agentMessage", "phase": "commentary",
        "text": commentary}}}]
    assert bridge.status("thread-1")["events"][0]["item"]["text"] == commentary


def test_status_subscriber_is_signalled_by_live_activity():
    bridge = CodexBridge()

    async def check():
        queue = bridge.subscribe("thread-1")
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        reader.feed_data((json.dumps({"method": "item/completed", "params": {
            "threadId": "thread-1", "item": {"type": "agentMessage", "phase": "commentary",
                                                   "text": "Still working."}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()
        assert await queue.get() == "item/completed"
        bridge.unsubscribe("thread-1", queue)
        assert "thread-1" not in bridge.status_subscribers

    asyncio.run(check())


def test_token_usage_notification_is_exposed_for_the_existing_context_meter():
    bridge = CodexBridge()

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        bridge.events["thread-1"] = asyncio.Queue()
        reader.feed_data((json.dumps({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                "total": {"totalTokens": 12345}, "last": {"totalTokens": 345},
                "modelContextWindow": 200000}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()

    asyncio.run(check())
    context = bridge.status("thread-1")["context"]
    assert context["used_tokens"] == 345
    assert context["context_window"] == 200000
    assert context["turn_id"] == "turn-1"
    assert context["updated_at"]


def test_tool_result_stays_pending_until_the_following_model_usage_step():
    bridge = CodexBridge()

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        messages = [
            {"method": "item/completed", "params": {"threadId": "thread-1",
                "turnId": "turn-1", "item": {"type": "commandExecution"}}},
            # App-server sends the response's count after draining its tool.
            {"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                    "last": {"totalTokens": 400}, "modelContextWindow": 200000}}},
        ]
        reader.feed_data(b"".join((json.dumps(message) + "\n").encode() for message in messages))
        reader.feed_eof()
        await bridge._read()

    asyncio.run(check())
    assert bridge.status("thread-1")["context_pending"] is True

    async def next_step():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        reader.feed_data((json.dumps({"method": "thread/tokenUsage/updated", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {
                "last": {"totalTokens": 725}, "modelContextWindow": 200000}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()

    asyncio.run(next_step())
    assert bridge.status("thread-1")["context_pending"] is False
    assert bridge.status("thread-1")["context"]["used_tokens"] == 725


def test_rate_limit_notification_is_exposed_for_dashboard_alerts():
    bridge = CodexBridge()

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        reader.feed_data((json.dumps({"method": "account/rateLimits/updated", "params": {
            "rateLimits": {"primary": {"usedPercent": 76,
                "windowDurationMins": 300, "resetsAt": 1800000000}}}}) + "\n").encode())
        reader.feed_eof()
        await bridge._read()

    asyncio.run(check())
    assert bridge.status("unused")["rate_limits"]["primary"] == {
        "usedPercent": 76, "windowDurationMins": 300, "resetsAt": 1800000000}


def test_delegate_activity_exposes_child_receipt_without_task_or_answer():
    bridge = CodexBridge()
    receipt = {"receipt_id": "child-123", "agent_type": "research",
               "child_route": "gpu-rig/shared-child", "duration_s": 4.2,
               "exit_code": 0, "roster_stable": True,
               "answer": "private answer", "task": "private prompt"}
    bridge.recent["thread-1"] = [{"method": "item/completed", "params": {
        "item": {"id": "call-1", "type": "mcpToolCall", "server": "local_agents",
                 "tool": "delegate_task", "status": "completed",
                 "result": {"content": [{"type": "text", "text": json.dumps(receipt)}]}}}}]
    public = bridge.status("thread-1")["events"][0]["item"]
    assert public == {"id": "call-1", "type": "mcpToolCall", "server": "local_agents",
                      "tool": "delegate_task", "status": "completed",
                      "child": {key: receipt[key] for key in (
                          "receipt_id", "agent_type", "child_route", "duration_s",
                          "exit_code", "roster_stable")}}
    assert _delegate_receipt({"content": [{"text": "not JSON"}]}) is None


def test_streamed_text_deltas_do_not_evict_tool_activity():
    bridge = CodexBridge()

    async def check():
        reader = asyncio.StreamReader()
        bridge.process = SimpleNamespace(stdout=reader)
        bridge.events["thread-1"] = asyncio.Queue()
        packets = [{"method": "item/completed", "params": {"threadId": "thread-1",
                    "item": {"id": "call-1", "type": "mcpToolCall", "server": "local_agents",
                             "tool": "delegate_task", "status": "completed"}}}]
        packets.extend({"method": "item/agentMessage/delta", "params": {
            "threadId": "thread-1", "delta": "word"}} for _ in range(120))
        reader.feed_data(('\n'.join(json.dumps(packet) for packet in packets) + '\n').encode())
        reader.feed_eof()
        await bridge._read()
        activity = bridge.status("thread-1")["events"]
        assert len(activity) == 1
        assert activity[0]["item"]["tool"] == "delegate_task"

    asyncio.run(check())


def test_codex_preferences_and_activity_survive_store_reopen(tmp_path):
    store = ChatStore(tmp_path)
    conversation = store.create("Codex", "codex", str(tmp_path))
    cid = conversation["id"]
    store.set_codex_settings(cid, "gpt-test", "medium", "auto_review")
    turn_id = store.begin_turn(cid, 0, "Codex", "Fix it")
    activity = [{"method": "item/completed", "item": {"id": "step-1", "type": "commandExecution",
                                                      "command": "pytest"}}]
    store.append_exchange(cid, 0, "Codex", "Fix it", "Done.", turn_id=turn_id, activity=activity,
                          context_length=200000, total_tokens=12345)
    restored = ChatStore(tmp_path).get(cid)
    assert (restored["codex_model"], restored["codex_effort"], restored["codex_reviewer"]) == (
        "gpt-test", "medium", "auto_review")
    assert restored["codex_access"] == "local"
    assert (restored["last_total_tokens"], restored["context_length"]) == (12345, 200000)
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
        "model": "Codex", "cwd": str(tmp_path), "mode": "coding"}).json()
    path = f"/dashboard/api/chat/conversations/{conversation['id']}/codex/settings"
    with pytest.raises(ManagerError, match="Reasoning effort is not available"):
        client.post(path, json={"model": "gpt-test", "effort": "high", "reviewer": "auto_review"})
    response = client.post(path, json={"model": "gpt-test", "effort": "medium", "reviewer": "auto_review"})
    assert response.status_code == 200, response.text
    saved = client.get(f"/dashboard/api/chat/conversations/{conversation['id']}").json()
    assert (saved["codex_model"], saved["codex_effort"], saved["codex_reviewer"]) == (
        "gpt-test", "medium", "auto_review")
    response = client.post(path, json={"model": "gpt-test", "effort": "medium",
                                      "reviewer": "auto_review", "access": "workspace"})
    assert response.status_code == 200, response.text
    assert response.json()["codex_access"] == "workspace"
