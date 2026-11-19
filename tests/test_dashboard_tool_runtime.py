"""Local dashboard chats use the imported catalog and the MCP argument schemas."""

from llm_gateway.dashboard import dashboard_mcp_servers, tool_runtime
import pytest


def test_local_parent_discovers_and_calls_reviewed_tool(tmp_path, monkeypatch):
    script = tmp_path / "mcp.py"
    script.write_text("""import json, sys
request = json.loads(sys.stdin.readline())
if request['method'] == 'tools/list':
    result = {'tools': [
        {'name': 'list_local_agents', 'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'delegate_task', 'description': 'Delegate work', 'inputSchema': {
            'type': 'object', 'properties': {'agent_type': {'type': 'string'},
                                            'task': {'type': 'string'}},
            'required': ['agent_type', 'task']}}]}
else:
    result = {'structuredContent': {'answer': '42'}}
print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}))
""")
    monkeypatch.setenv("CODEX_LOCAL_AGENT_MCP_SCRIPT", str(script))
    monkeypatch.setenv("CODEX_LOCAL_AGENT_RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.delenv("CODEX_SEARCH_MCP_SCRIPT", raising=False)
    advertised, dispatch = tool_runtime.available("gpu-rig/example", frozenset({"local_agents"}))
    assert [item["function"]["name"] for item in advertised] == ["list_local_agents", "delegate_task"]
    assert tool_runtime.call(dispatch, "delegate_task", {"agent_type": "coding", "task": "7 times 6"}) == {
        "structuredContent": {"answer": "42"}}
    try:
        tool_runtime.call(dispatch, "delegate_task", {"agent_type": "coding", "task": "x", "extra": 1})
    except tool_runtime.DashboardToolError:
        pass
    else:
        raise AssertionError("unpublished tool argument was accepted")


def test_mcp_tool_error_keeps_bounded_reason(monkeypatch):
    monkeypatch.setattr(tool_runtime, "_request", lambda *args, **kwargs: {
        "isError": True, "content": [{"type": "text", "text": "workspace path is not visible"}]})
    dispatch = {"list_workspace_files": (["fixture"], {"properties": {"path": {}}})}
    with pytest.raises(tool_runtime.DashboardToolError, match="workspace path is not visible"):
        tool_runtime.call(dispatch, "list_workspace_files", {"path": "../"})


def test_search_launcher_rejects_non_loopback_endpoint(tmp_path, monkeypatch):
    script = tmp_path / "search.py"
    script.write_text("# fixture\n")
    config = tmp_path / "search.env"
    config.write_text("RIG_SEARCH_PROVIDERS=searxng\n")
    monkeypatch.setenv("CODEX_SEARCH_MCP_SCRIPT", str(script))
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENV_FILE", str(config))
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENDPOINT", "https://example.com/search")
    with pytest.raises(tool_runtime.DashboardToolError, match="loopback"):
        tool_runtime._launch("rig_search", "gpu-rig/example")


def test_rig_info_launcher_uses_explicit_harness_script(tmp_path, monkeypatch):
    script = tmp_path / "rig_info_mcp.py"
    script.write_text("# read-only fixture\n")
    monkeypatch.setenv("CODEX_RIG_INFO_MCP_SCRIPT", str(script))
    command = tool_runtime._launch("rig_info", "gpu-rig/example")
    assert command[-1] == str(script)


def test_local_coding_tools_are_scoped_to_selected_workspace(tmp_path, monkeypatch):
    agent = tmp_path / "gateway_subagent_mcp.py"
    reader = tmp_path / "workspace_read_mcp.py"
    agent.write_text("# fixture\n")
    reader.write_text("# fixture\n")
    monkeypatch.setenv("CODEX_LOCAL_AGENT_MCP_SCRIPT", str(agent))
    monkeypatch.setenv("CODEX_LOCAL_AGENT_RECEIPTS", str(tmp_path / "receipts"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    delegated = tool_runtime._launch("local_agents", "gpu-rig/example", workspace)
    assert delegated[-3:] == ["--workspace-root", str(workspace), "--coding-only"]
    reading = tool_runtime._launch("workspace_read", "gpu-rig/example", workspace)
    assert reading[-2:] == ["--root", str(workspace)]
    assert tool_runtime._launch("workspace_read", "gpu-rig/example") is None


def test_cloud_and_local_dashboard_use_same_catalog_and_servers(tmp_path, monkeypatch):
    agent = tmp_path / "local_agent_mcp.py"
    search = tmp_path / "search_mcp.py"
    rig_info = tmp_path / "rig_info_mcp.py"
    env_file = tmp_path / "search.env"
    for path in (agent, search, rig_info, env_file):
        path.write_text("# fixture\n")
    monkeypatch.setenv("CODEX_LOCAL_AGENT_MCP_SCRIPT", str(agent))
    monkeypatch.setenv("CODEX_LOCAL_AGENT_RECEIPTS", str(tmp_path / "receipts"))
    monkeypatch.setenv("CODEX_SEARCH_MCP_SCRIPT", str(search))
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENV_FILE", str(env_file))
    monkeypatch.setenv("CODEX_RIG_INFO_MCP_SCRIPT", str(rig_info))
    monkeypatch.setenv("CODEX_SEARCH_MCP_ENDPOINT", "http://127.0.0.1:8888")
    chat_cloud = dashboard_mcp_servers.cloud_config(mode="chat")["mcp_servers"]
    coding_cloud = dashboard_mcp_servers.cloud_config(mode="coding")["mcp_servers"]
    assert "rig_search" in dashboard_mcp_servers.mode_servers("chat")
    assert "workspace_read" not in dashboard_mcp_servers.mode_servers("chat")
    assert "workspace_read" in dashboard_mcp_servers.mode_servers("coding")
    assert "rig_search" not in dashboard_mcp_servers.mode_servers("coding")
    catalog = dashboard_mcp_servers.catalog_servers()
    for server in ("local_agents", "rig_search", "rig_info"):
        local = tool_runtime._launch(server, "gpu-rig/example")
        cloud = chat_cloud if server == "rig_search" else coding_cloud
        assert local[:2] == [cloud[server]["command"], cloud[server]["args"][0]]
        assert cloud[server]["enabled_tools"] == [row["name"] for row in catalog[server]["tools"]
                                                   if row["approval"] == "automatic"]
    assert "rig_search" not in coding_cloud
    assert "rig_info" in chat_cloud
