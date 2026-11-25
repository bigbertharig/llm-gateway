"""Run reviewed harness MCP tools for a local dashboard model turn."""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path
from . import dashboard_mcp_servers


class DashboardToolError(RuntimeError):
    pass


def _launch(server: str, parent_route: str, workspace: Path | None = None) -> list[str] | None:
    try:
        return dashboard_mcp_servers.launch(server, parent_route, workspace=workspace)
    except ValueError as exc:
        raise DashboardToolError(str(exc)) from exc


def _request(command: list[str], method: str, params: dict | None = None, timeout: int = 10) -> dict:
    packet = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        packet["params"] = params
    try:
        run = subprocess.run(command, input=json.dumps(packet) + "\n", text=True,
                             capture_output=True, timeout=timeout)
        if run.returncode or not run.stdout.strip():
            raise DashboardToolError(f"MCP {method} failed (exit {run.returncode})")
        response = json.loads(run.stdout.splitlines()[0])
        if response.get("id") != 1 or "result" not in response:
            raise DashboardToolError(f"MCP {method} returned an invalid response")
        return response["result"]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise DashboardToolError(f"MCP {method} failed: {type(exc).__name__}") from exc


def available(parent_route: str, allowed_servers: frozenset[str] | None = None,
              workspace: Path | None = None) -> tuple[list[dict], dict[str, tuple[list[str], dict]]]:
    """Use the gateway snapshot for permission and live tools/list for schemas."""
    try:
        catalog = dashboard_mcp_servers.catalog_servers()
    except ValueError as exc:
        raise DashboardToolError("Dashboard tool catalog is unavailable") from exc
    advertised, dispatch = [], {}
    allowed_servers = allowed_servers or frozenset()
    for server, entry in catalog.items():
        if allowed_servers is not None and server not in allowed_servers:
            continue
        command = _launch(server, parent_route, workspace)
        if command is None:
            continue
        live = _request(command, "tools/list")
        schemas = {tool["name"]: tool for tool in live.get("tools", [])}
        for row in entry["tools"]:
            name = row["name"]
            if not dashboard_mcp_servers.tool_enabled(server, row):
                continue
            if name not in schemas or name in dispatch:
                raise DashboardToolError(f"Catalog and MCP tools disagree on {server}.{name}")
            schema = schemas[name]
            digest = hashlib.sha256(json.dumps(schema.get('inputSchema'),sort_keys=True,separators=(',',':')).encode()).hexdigest()
            expected = row.get('input_schema_sha256')
            if not expected or expected != digest:
                raise DashboardToolError(f'Reviewed and live MCP schemas disagree on {server}.{name}; refresh the reviewed catalog')
            advertised.append({"type": "function", "function": {
                "name": name, "description": schema.get("description", ""),
                "parameters": schema["inputSchema"]}})
            dispatch[name] = (command, schema["inputSchema"])
    return advertised, dispatch


def call(dispatch: dict, name: str, arguments: dict) -> dict:
    if name not in dispatch or not isinstance(arguments, dict):
        raise DashboardToolError("Tool is unavailable")
    command, schema = dispatch[name]
    if set(arguments) - set(schema.get("properties", {})) or any(
            field not in arguments for field in schema.get("required", [])):
        raise DashboardToolError("Tool arguments do not match the published schema")
    result = _request(command, "tools/call", {"name": name, "arguments": arguments}, timeout=480)
    if result.get("isError"):
        details = result.get("content") or []
        reason = next((item.get("text", "") for item in details
                       if isinstance(item, dict) and item.get("type") == "text"), "")
        raise DashboardToolError(f"Tool {name} reported an error" +
                                 (f": {reason[:240]}" if isinstance(reason, str) and reason else ""))
    return result
