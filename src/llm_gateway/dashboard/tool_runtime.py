"""Run reviewed harness MCP tools for a local dashboard model turn."""

from __future__ import annotations

import json
import subprocess
from . import dashboard_mcp_servers


class DashboardToolError(RuntimeError):
    pass


def _launch(server: str, parent_route: str) -> list[str] | None:
    try:
        return dashboard_mcp_servers.launch(server, parent_route)
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


def available(parent_route: str) -> tuple[list[dict], dict[str, tuple[list[str], dict]]]:
    """Use the gateway snapshot for permission and live tools/list for schemas."""
    try:
        catalog = dashboard_mcp_servers.catalog_servers()
    except ValueError as exc:
        raise DashboardToolError("Dashboard tool catalog is unavailable") from exc
    advertised, dispatch = [], {}
    for server, entry in catalog.items():
        command = _launch(server, parent_route)
        if command is None:
            continue
        live = _request(command, "tools/list")
        schemas = {tool["name"]: tool for tool in live.get("tools", [])}
        for row in entry["tools"]:
            name = row["name"]
            if row["approval"] != "automatic":
                continue
            if name not in schemas or name in dispatch:
                raise DashboardToolError(f"Catalog and MCP tools disagree on {server}.{name}")
            schema = schemas[name]
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
        raise DashboardToolError(f"Tool {name} reported an error")
    return result
