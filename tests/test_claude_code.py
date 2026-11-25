"""Claude Code subscription bridge contracts without external model calls."""

import asyncio
import json

from llm_gateway.dashboard.claude_code import ClaudeCodeBridge


def test_account_status_reports_subscription_without_credentials(tmp_path):
    bridge = ClaudeCodeBridge()

    async def run(*args, **kwargs):
        return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                              "email": "operator@example.test", "subscriptionType": "pro"}), ""

    bridge._run = run
    status = asyncio.run(bridge.account())
    assert status["connected"] is True
    assert status["auth_method"] == "claude.ai"
    assert "token" not in json.dumps(status).lower()


def test_new_and_resumed_turns_use_persistent_claude_session(tmp_path, monkeypatch):
    bridge = ClaudeCodeBridge()
    calls = []
    monkeypatch.setattr("llm_gateway.dashboard.claude_code.dashboard_mcp_servers.cloud_config",
                        lambda *args, **kwargs: {"mcp_servers": {}})

    async def run(*args, **kwargs):
        calls.append(args)
        return 0, json.dumps({"type": "result", "subtype": "success", "result": "Done.",
                              "session_id": "11111111-1111-4111-8111-111111111111",
                              "usage": {"input_tokens": 5, "output_tokens": 2}}), ""

    bridge._run = run
    first = asyncio.run(bridge.turn(None, "First", workspace=tmp_path))
    second = asyncio.run(bridge.turn(first["session_id"], "Second", workspace=tmp_path))
    assert "--session-id" in calls[0] and "--resume" not in calls[0]
    assert "--resume" in calls[1]
    assert second["answer"] == "Done."
