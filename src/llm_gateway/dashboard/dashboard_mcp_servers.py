"""One launch and permission contract for local and cloud dashboard turns."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

TOOL_CATALOG = Path(__file__).with_name("tool_catalog.json")
MODE_SERVERS = {
    "chat": frozenset({"local_agents", "rig_info", "rig_search", "google_workspace", "github_public"}),
    "coding": frozenset({"local_agents", "rig_info", "workspace_read", "workspace_git"}),
}


def catalog_servers() -> dict:
    try:
        data = json.loads(TOOL_CATALOG.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("servers"), dict):
            raise ValueError("unsupported catalog")
        return data["servers"]
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Dashboard tool catalog is unavailable; sync from harness") from exc


def mode_servers(mode: str) -> frozenset[str]:
    """Select the permitted surfaces from the one harness-owned dashboard catalog."""
    if mode not in MODE_SERVERS:
        raise ValueError("invalid dashboard tool mode")
    return MODE_SERVERS[mode] & catalog_servers().keys()


def surface_servers(mode: str, surface: str = "local") -> frozenset[str]:
    """Keep mobile tool grants independent from the local conversation settings."""
    base = mode_servers(mode)
    if surface == "local":
        return base
    if surface != "mobile":
        raise ValueError("invalid dashboard surface")
    configured = {name.strip() for name in os.getenv(
        "MOBILE_TOOL_SERVERS", "rig_info,rig_search,workspace_read").split(",")}
    return base & configured


def launch(server: str, parent_route: str | None = None, *, cloud: bool = False,
           workspace: Path | None = None, conversation_id: str | None = None) -> list[str] | None:
    """Build the same harness MCP server for either dashboard model family."""
    if server == "local_agents":
        configured = os.getenv("CODEX_LOCAL_AGENT_MCP_SCRIPT")
        if not configured:
            return None
        script = Path(configured).resolve()
        if not script.is_file():
            raise ValueError("Local agent MCP script is unavailable")
        receipts = Path(os.getenv("CODEX_LOCAL_AGENT_RECEIPTS") or
                        (Path.home() / ".local/share/llm-gateway/local-agent-receipts"))
        receipts.mkdir(parents=True, exist_ok=True)
        parent = ["--cloud-parent"] if cloud else ["--parent-route", str(parent_route)]
        scope = (["--workspace-root", str(workspace), "--coding-only"]
                 if workspace is not None else [])
        return [sys.executable, str(script), *parent, "--receipt-dir", str(receipts), *scope]
    if server == "rig_search":
        configured = os.getenv("CODEX_SEARCH_MCP_SCRIPT")
        if not configured:
            return None
        script = Path(configured).resolve()
        env_file = os.getenv("CODEX_SEARCH_MCP_ENV_FILE")
        if not script.is_file() or not env_file or not Path(env_file).is_file():
            raise ValueError("Search MCP provider config is unavailable")
        endpoint = os.getenv("CODEX_SEARCH_MCP_ENDPOINT", "http://127.0.0.1:8888")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Search MCP endpoint must be Pi loopback HTTP")
        return [sys.executable, str(script), "--env-file", env_file, "--endpoint", endpoint]
    if server == "rig_info":
        configured = os.getenv("CODEX_RIG_INFO_MCP_SCRIPT")
        if not configured and os.getenv("CODEX_LOCAL_AGENT_MCP_SCRIPT"):
            sibling = Path(os.environ["CODEX_LOCAL_AGENT_MCP_SCRIPT"]).with_name("rig_info_mcp.py")
            configured = str(sibling) if sibling.is_file() else None
        if not configured:
            return None
        script = Path(configured).resolve()
        if not script.is_file():
            raise ValueError("Rig information MCP script is unavailable")
        return [sys.executable, str(script)]
    if server == "google_workspace":
        if os.getenv("GOOGLE_WORKSPACE_ENABLED") != "1":
            return None
        script = Path(os.getenv("GOOGLE_WORKSPACE_MCP_SCRIPT") or
                      "/home/bryan/llm-harness/tools/google_workspace_mcp.py").resolve()
        if not script.is_file():
            raise ValueError("Google Workspace MCP script is unavailable")
        return [sys.executable, str(script)]
    if server == "github_public":
        script = Path(os.getenv("GITHUB_PUBLIC_MCP_SCRIPT") or
                      "/home/bryan/llm-harness/tools/github_public_mcp.py").resolve()
        if not script.is_file():
            raise ValueError("Public GitHub MCP script is unavailable")
        return [sys.executable, str(script)]
    if server in ("local_control", "private_control"):
        script = Path(os.getenv("CODEX_HOST_CONTROL_MCP_SCRIPT") or
                      "/home/bryan/llm-harness/tools/host_control_mcp.py").resolve()
        if not script.is_file():
            raise ValueError("Host control MCP script is unavailable")
        return [sys.executable, str(script), "--scope",
                "local" if server == "local_control" else "full"]
    if server == "workspace_read":
        if workspace is None:
            return None
        configured = os.getenv("CODEX_WORKSPACE_READ_MCP_SCRIPT")
        if not configured and os.getenv("CODEX_LOCAL_AGENT_MCP_SCRIPT"):
            sibling = Path(os.environ["CODEX_LOCAL_AGENT_MCP_SCRIPT"]).with_name("workspace_read_mcp.py")
            configured = str(sibling) if sibling.is_file() else None
        if not configured:
            return None
        script = Path(configured).resolve()
        if not script.is_file():
            raise ValueError("Workspace read MCP script is unavailable")
        return [sys.executable, str(script), "--root", str(workspace)]
    if server == "workspace_git":
        if workspace is None:
            return None
        script = Path(os.getenv("CODEX_WORKSPACE_GIT_MCP_SCRIPT") or
                      "/home/bryan/llm-harness/tools/workspace_git_mcp.py").resolve()
        if not script.is_file():
            raise ValueError("Workspace Git MCP script is unavailable")
        return [sys.executable, str(script), "--root", str(workspace)]
    if server == "gateway_reset":
        if not cloud or not conversation_id or not conversation_id.isalnum():
            return None
        script = Path(os.getenv("CODEX_GATEWAY_RESET_MCP_SCRIPT") or
                      "/home/bryan/llm-harness/tools/gateway_reset_mcp.py").resolve()
        if not script.is_file():
            raise ValueError("Gateway reset MCP script is unavailable")
        data_root = Path(os.getenv("CHAT_DATA_ROOT") or
                         (Path.home() / ".local/share/llm-gateway/chat")).resolve()
        return [sys.executable, str(script), "--conversation-id", conversation_id,
                "--data-root", str(data_root)]
    return None


def cloud_config(workspace: Path | None = None, *, mode: str = 'coding',
                 conversation_id: str | None = None, surface: str = "local") -> dict:
    allowed = surface_servers(mode, surface) | ({"gateway_reset"} if conversation_id and surface == "local" else set())
    servers = {}
    for name, entry in catalog_servers().items():
        if name not in allowed:
            continue
        command = launch(name, cloud=True, workspace=workspace, conversation_id=conversation_id)
        if command is None:
            continue
        enabled = [row["name"] for row in entry["tools"] if row["approval"] == "automatic"]
        servers[name] = {"command": command[0], "args": command[1:],
                         "enabled_tools": enabled,
                         "tool_timeout_sec": 480 if name == "local_agents" else 60 if name == "rig_search" else 30}
    return {"mcp_servers": servers}
