"""One launch and permission contract for local and cloud dashboard turns."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

TOOL_CATALOG = Path(__file__).with_name("tool_catalog.json")


def catalog_servers() -> dict:
    try:
        data = json.loads(TOOL_CATALOG.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("servers"), dict):
            raise ValueError("unsupported catalog")
        return data["servers"]
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Dashboard tool catalog is unavailable; sync from harness") from exc


def launch(server: str, parent_route: str | None = None, *, cloud: bool = False) -> list[str] | None:
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
        return [sys.executable, str(script), *parent, "--receipt-dir", str(receipts)]
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
    return None


def cloud_config() -> dict:
    servers = {}
    for name, entry in catalog_servers().items():
        command = launch(name, cloud=True)
        if command is None:
            continue
        enabled = [row["name"] for row in entry["tools"] if row["approval"] == "automatic"]
        servers[name] = {"command": command[0], "args": command[1:],
                         "enabled_tools": enabled,
                         "tool_timeout_sec": 480 if name == "local_agents" else 60 if name == "rig_search" else 30}
    return {"mcp_servers": servers}
