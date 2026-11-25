"""Bounded Claude Code subscription bridge for dashboard conversations."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from pathlib import Path

from . import dashboard_mcp_servers


class ClaudeCodeError(RuntimeError):
    pass


class ClaudeCodeBridge:
    def __init__(self, command: str | None = None):
        self.command = command or os.getenv("CLAUDE_CODE_COMMAND", "claude")
        self.login_process: asyncio.subprocess.Process | None = None
        self.login_lock = asyncio.Lock()

    async def _run(self, *args: str, cwd: Path | None = None, timeout: float = 900) -> tuple[int, str, str]:
        if not shutil.which(self.command):
            raise ClaudeCodeError("Claude Code is not installed on the gateway host")
        try:
            process = await asyncio.create_subprocess_exec(
                self.command, *args, cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise ClaudeCodeError("Claude Code timed out") from exc
        except OSError as exc:
            raise ClaudeCodeError("Claude Code could not start") from exc
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def account(self) -> dict:
        try:
            code, stdout, stderr = await self._run("auth", "status", "--json", timeout=20)
            value = json.loads(stdout or "{}")
        except (ClaudeCodeError, ValueError):
            return {"connected": False, "error": "Claude Code account status is unavailable"}
        if code != 0:
            return {"connected": False, "error": (stderr.strip() or "Claude Code is not signed in")[:300]}
        connected = value.get("loggedIn") is True
        return {"connected": connected,
                "auth_method": value.get("authMethod") if connected else None,
                "email": value.get("email") if connected else None,
                "organization": value.get("orgName") if connected else None,
                "subscription": value.get("subscriptionType") if connected else None}

    async def logout(self) -> None:
        await self.cancel_login()
        code, _stdout, stderr = await self._run("auth", "logout", timeout=30)
        if code != 0:
            raise ClaudeCodeError((stderr.strip() or "Claude Code sign-out failed")[:300])

    async def start_login(self) -> dict:
        async with self.login_lock:
            await self.cancel_login()
            if not shutil.which(self.command):
                raise ClaudeCodeError("Claude Code is not installed on the gateway host")
            env = {**os.environ, "BROWSER": "true"}
            try:
                self.login_process = await asyncio.create_subprocess_exec(
                    self.command, "auth", "login", "--claudeai", env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                deadline = asyncio.get_running_loop().time() + 30
                while asyncio.get_running_loop().time() < deadline:
                    line = await asyncio.wait_for(self.login_process.stdout.readline(), 10)
                    if not line:
                        break
                    match = re.search(r"https://[^\s]+", line.decode(errors="replace"))
                    if match and "/oauth/authorize" in match.group(0):
                        return {"verification_url": match.group(0)}
            except (OSError, asyncio.TimeoutError) as exc:
                await self.cancel_login()
                raise ClaudeCodeError("Claude Code sign-in could not start") from exc
            await self.cancel_login()
            raise ClaudeCodeError("Claude Code did not provide a sign-in URL")

    async def complete_login(self, code: str) -> dict:
        code = code.strip()
        if not code or "\n" in code or len(code) > 4096:
            raise ClaudeCodeError("Paste the one-time Claude authorization code")
        async with self.login_lock:
            process = self.login_process
            if process is None or process.returncode is not None:
                raise ClaudeCodeError("Start Claude Code sign-in again")
            try:
                stdout, _ = await asyncio.wait_for(process.communicate((code + "\n").encode()), 60)
            except asyncio.TimeoutError as exc:
                await self.cancel_login()
                raise ClaudeCodeError("Claude Code sign-in timed out") from exc
            finally:
                self.login_process = None
            if process.returncode != 0:
                raise ClaudeCodeError((stdout.decode(errors="replace").strip() or
                                       "Claude Code rejected the authorization code")[-500:])
            return await self.account()

    async def cancel_login(self) -> None:
        process, self.login_process = self.login_process, None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def close(self) -> None:
        await self.cancel_login()

    async def turn(self, session_id: str | None, prompt: str, *, workspace: Path,
                   mode: str = "chat", surface: str = "local") -> dict:
        existing_session = bool(session_id)
        session_id = session_id or str(uuid.uuid4())
        session = ["--resume", session_id] if existing_session else ["--session-id", session_id]
        # Safe mode excludes personal hooks/plugins/settings. Restricted mode confines
        # built-in file tools to the selected dashboard workspace and removes shell/WebFetch.
        gateway_config = dashboard_mcp_servers.cloud_config(
            workspace if mode == "coding" else None, mode=mode, surface=surface)
        servers, allowed = {}, []
        for name, value in gateway_config.get("mcp_servers", {}).items():
            servers[name] = {"type": "stdio", "command": value["command"], "args": value["args"]}
            allowed.extend("mcp__" + name + "__" + tool for tool in value.get("enabled_tools", []))
        args = ["--print", "--output-format", "json", "--safe-mode", "--restricted",
                "--permission-mode", "auto", "--permission-prompts", "none",
                "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": servers})]
        if allowed:
            args.extend(["--allowedTools", ",".join(allowed)])
        args.extend([*session, prompt])
        code, stdout, stderr = await self._run(*args, cwd=workspace)
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            raise ClaudeCodeError((stderr.strip() or "Claude Code returned invalid output")[:300]) from exc
        if code != 0 or result.get("is_error") or result.get("subtype") not in (None, "success"):
            message = result.get("result") or stderr.strip() or "Claude Code turn failed"
            raise ClaudeCodeError(str(message)[:500])
        answer = result.get("result")
        if not isinstance(answer, str) or not answer.strip():
            raise ClaudeCodeError("Claude Code returned no user-facing answer")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return {"answer": answer.strip(), "session_id": result.get("session_id") or session_id,
                "usage": usage, "model": "Claude Code"}
