"""Private stdio bridge to Codex app-server for dashboard cloud conversations.

The process runs as the gateway service user on the Pi. Its authentication store
never crosses the dashboard API; only account display state and a device code do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import dashboard_mcp_servers

log = logging.getLogger(__name__)
RECENT_EVENT_LIMIT = 1000
_APP_SERVER_SECRET = re.compile(
    r"(?i)(authorization:\s*bearer\s+|access[_-]?token[=:]\s*|api[_-]?key[=:]\s*)\S+")


def _safe_app_server_log(line: str) -> str:
    return _APP_SERVER_SECRET.sub(r"\1[redacted]", line.replace("\x00", ""))[:2000]


class CodexBridgeError(RuntimeError):
    pass


class CodexUsageLimitError(CodexBridgeError):
    """Structured signal emitted only for app-server's usage-limit error code."""

    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


class CodexTurnInterrupted(CodexBridgeError):
    pass


class CodexAppServerExited(CodexBridgeError):
    """The supervised stdio child vanished while a turn was active."""

    pass


_ACTIVE_TURN_MISMATCH = re.compile(
    r"expected active turn id\s+[`'\"]?([0-9a-f-]+)[`'\"]?\s+"
    r"but found\s+[`'\"]?([0-9a-f-]+)[`'\"]?",
    re.IGNORECASE,
)


def _reported_active_turn(error: Exception) -> str | None:
    """Extract app-server's authoritative active turn from a mismatch error."""
    match = _ACTIVE_TURN_MISMATCH.search(str(error))
    return match.group(2) if match else None


def _delegate_receipt(value: object) -> dict | None:
    """Expose bounded child metadata from an MCP result, never its task/answer."""
    if isinstance(value, str) and len(value) <= 32000:
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, list):
        for entry in value[:8]:
            found = _delegate_receipt(entry)
            if found:
                return found
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("child_route"), str):
        allowed = ("receipt_id", "agent_type", "child_route", "child_model", "duration_s",
                   "tool_profile", "tool_mode", "tool_calls",
                   "exit_code", "roster_stable", "released", "error")
        return {key: value[key] for key in allowed if key in value and
                isinstance(value[key], (str, int, float, bool, type(None)))}
    for key in ("structuredContent", "result", "content", "text"):
        if key in value:
            found = _delegate_receipt(value[key])
            if found:
                return found
    return None


class CodexBridge:
    def __init__(self, command: str | None = None):
        self.command = command or os.getenv("CODEX_CHAT_COMMAND", "codex")
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.stderr_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: dict[str, asyncio.Queue] = {}
        self.active_turns: dict[str, str] = {}
        self.recent: dict[str, deque] = {}
        self.token_usage: dict[str, dict[str, object]] = {}
        # App-server emits the usage for a model response after its tools have
        # drained.  That first notification still predates the tool result;
        # the result is counted by the following model request.
        self.context_pending: dict[str, bool] = {}
        self._skip_pending_clear: set[str] = set()
        self.status_subscribers: dict[str, set[asyncio.Queue]] = {}
        self.rate_limits: dict = {}
        self.approvals: dict[str, dict] = {}
        self.login_event: dict | None = None
        self.account_state: dict | None = None
        self.compaction_listener = None
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self._closing = False
        self.workspaces = [Path(p).resolve() for p in os.getenv(
            "CODEX_CHAT_WORKSPACES", "/home/bryan/llm-gateway:/home/bryan/llm_orchestration:/home/bryan/llm-harness"
        ).split(":") if p]
        self.research_root = Path(os.getenv("CODEX_CHAT_RESEARCH_ROOT") or
                                  (Path(os.getenv("CHAT_DATA_ROOT", str(Path.home() / ".local/share/llm-gateway/chat"))) /
                                   "codex-chat-research"))

    def _remember_recent(self, thread_id: str, event: dict) -> None:
        """Bound tool noise without evicting assistant commentary first."""
        events = self.recent.setdefault(thread_id, deque())
        events.append(event)
        while len(events) > RECENT_EVENT_LIMIT:
            removable = next((index for index, saved in enumerate(events)
                              if ((saved.get("params") or {}).get("item") or {}).get("type")
                              != "agentMessage"), None)
            if removable is None:
                events.popleft()
            else:
                del events[removable]

    def workspace(self, path: str) -> Path:
        selected = Path(path).resolve()
        if selected not in self.workspaces or not selected.is_dir():
            raise CodexBridgeError("Choose an available coding workspace")
        return selected

    def research_workspace(self, path: str | None = None) -> Path:
        root = self.research_root
        if root.is_symlink():
            raise CodexBridgeError("Chat research directory cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not root.is_dir():
            raise CodexBridgeError("Chat research directory is unavailable")
        selected = root.resolve()
        if path is not None and Path(path).resolve() != selected:
            raise CodexBridgeError("Chat research directory changed")
        marker = root / ".dashboard-research-root"
        if marker.is_symlink() or (marker.exists() and not marker.is_file()):
            raise CodexBridgeError("Chat research directory marker is invalid")
        if not marker.exists() and any(root.iterdir()):
            raise CodexBridgeError("Chat research directory must be empty")
        if not marker.exists():
            marker.touch(exist_ok=True)
            marker.chmod(0o600)
        return selected

    def dashboard_tools(self, server: str) -> list[dict]:
        """Read gateway's imported harness catalog, never the harness checkout."""
        try:
            return dashboard_mcp_servers.catalog_servers()[server]["tools"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CodexBridgeError("Dashboard tool catalog is unavailable; sync from harness") from exc

    def automatic_tool_request(self, params: dict) -> bool:
        if params.get("mode") != "form":
            return False
        server = params.get("serverName")
        if not isinstance(server, str) or not server.isidentifier():
            return False
        try:
            tools = self.dashboard_tools(server)
        except CodexBridgeError:
            return False
        return any(tool["approval"] == "automatic" and
                   params.get("message") == f'Allow the {server} MCP server to run tool "{tool["name"]}"?'
                   for tool in tools)

    def local_agent_config(self) -> dict:
        """Optional harness MCP adapter for bounded local children of a Codex session."""
        return self._server_config("local_agents")

    def search_config(self) -> dict:
        """Optional Pi-local research tools for Codex sessions."""
        return self._server_config("rig_search")

    def rig_info_config(self) -> dict:
        """Read-only harness view of the dashboard's own status sources."""
        return self._server_config("rig_info")

    def _server_config(self, server: str) -> dict:
        try:
            config = dashboard_mcp_servers.cloud_config(mode='chat' if server == 'rig_search' else 'coding')["mcp_servers"]
        except ValueError as exc:
            raise CodexBridgeError(str(exc)) from exc
        return {"mcp_servers": {server: config[server]}} if server in config else {}

    async def start(self) -> None:
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            self._closing = False
            try:
                research_root = self.research_workspace()
                # A named profile is required by current app-server for restricted reads.
                profile = ('permissions.gateway_chat={filesystem={":minimal"="read",' +
                           json.dumps(str(research_root)) + '="read"},network={enabled=false}}')
                self.process = await asyncio.create_subprocess_exec(
                    self.command, "-c", 'default_permissions=":workspace"',
                    "-c", profile, "app-server", "--listen", "stdio://",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=4 * 1024 * 1024,
                )
            except OSError as exc:
                raise CodexBridgeError("Codex app-server is not installed on the gateway host") from exc
            self.reader_task = asyncio.create_task(self._read())
            self.stderr_task = asyncio.create_task(self._drain_stderr())
            await self._call("initialize", {"clientInfo": {
                "name": "llm_gateway_chat", "title": "Rig dashboard chat", "version": "0.1.0",
            }, "capabilities": {"experimentalApi": True}}, ensure_started=False)
            await self._write({"method": "initialized", "params": {}})

    async def close(self) -> None:
        self._closing = True
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self.reader_task:
            self.reader_task.cancel()
        if self.stderr_task:
            self.stderr_task.cancel()
        self.process = None

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            cleaned = _safe_app_server_log(line.decode("utf-8", "replace").rstrip())
            if cleaned:
                log.warning("Codex app-server: %s", cleaned)

    async def _write(self, message: dict) -> None:
        if not self.process or not self.process.stdin:
            raise CodexBridgeError("Codex app-server is unavailable")
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await self.process.stdin.drain()

    async def _call(self, method: str, params: dict | None = None, *, ensure_started: bool = True) -> dict:
        if ensure_started:
            await self.start()
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            response = await asyncio.wait_for(future, 60)
        except asyncio.TimeoutError as exc:
            raise CodexBridgeError(f"Codex app-server timed out on {method}") from exc
        finally:
            self.pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise CodexBridgeError(str(error.get("message", error)))
        return response.get("result") or {}

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        process = self.process
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                message["_received_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                if "method" not in message and "id" in message and message["id"] in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                    continue
                method, params = message.get("method"), message.get("params") or {}
                if method == "account/login/completed":
                    self.login_event = params
                    self.account_state = None
                if method == "account/rateLimits/updated":
                    update = params.get("rateLimits") or {}
                    if isinstance(update, dict):
                        # Notifications are intentionally sparse. Preserve the
                        # last full snapshot while replacing supplied windows.
                        self.rate_limits.update(update)
                thread_id = params.get("threadId")
                if thread_id and method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage") or {}
                    # `total` is lifetime thread spend and can be many times the
                    # context window. Codex's own TUI uses `last.totalTokens` as
                    # the latest active context size.
                    current = usage.get("last") or {}
                    used = current.get("totalTokens")
                    context_window = usage.get("modelContextWindow")
                    if (isinstance(used, int) and not isinstance(used, bool) and used >= 0
                            and isinstance(context_window, int) and not isinstance(context_window, bool)
                            and context_window > 0):
                        self.token_usage[thread_id] = {
                            "used_tokens": used, "context_window": context_window,
                            "updated_at": message["_received_at"],
                            "turn_id": params.get("turnId")}
                        if thread_id in self._skip_pending_clear:
                            self._skip_pending_clear.discard(thread_id)
                        else:
                            self.context_pending.pop(thread_id, None)
                if thread_id and method == "item/completed":
                    item = params.get("item") or {}
                    if isinstance(item, dict) and item.get("type") in {
                            "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall"}:
                        self.context_pending[thread_id] = True
                        self._skip_pending_clear.add(thread_id)
                if thread_id and method == "thread/compacted" and self.compaction_listener:
                    try:
                        self.compaction_listener(thread_id, message["_received_at"])
                    except Exception:
                        log.exception("could not persist Codex compaction boundary")
                if thread_id in self.events:
                    self.events[thread_id].put_nowait(message)
                # Keep durable activity items, not token deltas. A long streamed
                # answer otherwise evicts every tool call from the bounded deque.
                if thread_id and method in ("item/started", "item/completed", "turn/completed",
                                             "thread/compacted"):
                    self._remember_recent(thread_id, message)
                if "id" in message and method:
                    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                        self.approvals[str(message["id"])] = {"id": str(message["id"]),
                            "method": method, "thread_id": thread_id, "params": params}
                    elif method == "mcpServer/elicitation/request" and self.automatic_tool_request(params):
                        await self._write({"id": message["id"], "result": {"action": "accept", "content": {}}})
                    else:
                        log.warning("unsupported Codex app-server request: %s", method)
                        await self._write({"id": message["id"], "error": {
                            "code": -32601, "message": "This dashboard does not support the requested interaction"}})
                if method == "serverRequest/resolved":
                    self.approvals.pop(str(params.get("requestId")), None)
                if thread_id and method in {
                        "item/started", "item/completed", "turn/completed",
                        "thread/tokenUsage/updated", "thread/compacted", "serverRequest/resolved"}:
                    for subscriber in tuple(self.status_subscribers.get(thread_id, ())):
                        if subscriber.empty():
                            subscriber.put_nowait(method)
        finally:
            if getattr(process, "returncode", 0) is None and hasattr(process, "wait"):
                try:
                    await asyncio.wait_for(process.wait(), 0.5)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 1)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            if self.process is process:
                self.process = None
            if not self._closing:
                log.error("Codex app-server exited unexpectedly with status %s",
                          getattr(process, "returncode", "unknown"))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexBridgeError("Codex app-server exited"))
            for queue in self.events.values():
                queue.put_nowait({"method": "bridge/exited", "params": {}})

    async def account(self) -> dict:
        if self.account_state is not None:
            return {**self.account_state, "login": self.login_event}
        result = await self._call("account/read", {"refreshToken": False})
        account = result.get("account") or {}
        self.account_state = {"connected": account.get("type") == "chatgpt",
                              "email": account.get("email"), "plan": account.get("planType")}
        if self.account_state["connected"]:
            try:
                limits = await self._call("account/rateLimits/read", {})
                snapshot = limits.get("rateLimits") or {}
                if isinstance(snapshot, dict):
                    self.rate_limits = snapshot
            except CodexBridgeError:
                # Older app-server versions may not expose rate-limit reads.
                pass
        return {**self.account_state, "login": self.login_event}

    async def start_login(self) -> dict:
        self.login_event = None
        result = await self._call("account/login/start", {"type": "chatgptDeviceCode"})
        return {"login_id": result.get("loginId"),
                "verification_url": result.get("verificationUrl"),
                "user_code": result.get("userCode")}

    async def logout(self) -> None:
        await self._call("account/logout")
        self.account_state = None

    async def delete_thread(self, thread_id: str) -> None:
        await self._call("thread/delete", {"threadId": thread_id})
        self.recent.pop(thread_id, None)
        self.token_usage.pop(thread_id, None)
        self.context_pending.pop(thread_id, None)
        self._skip_pending_clear.discard(thread_id)

    async def compact(self, thread_id: str) -> None:
        await self._call("thread/compact/start", {"threadId": thread_id})

    async def models(self) -> list[dict]:
        result = await self._call("model/list", {"includeHidden": False, "limit": 100})
        return [item for item in result.get("data", []) if not item.get("hidden")]

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.status_subscribers.setdefault(thread_id, set()).add(queue)
        return queue

    def unsubscribe(self, thread_id: str, queue: asyncio.Queue) -> None:
        subscribers = self.status_subscribers.get(thread_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self.status_subscribers.pop(thread_id, None)

    def status(self, thread_id: str) -> dict:
        active = thread_id in self.events
        events = self.recent.get(thread_id, ())
        public_events = []
        pending = []
        for event in list(events):
            method, params = event.get("method"), event.get("params") or {}
            item = params.get("item") or {}
            if method == "thread/compacted":
                public_events.append({"method": method, "item": None, "turn": None,
                                      "received_at": event.get("_received_at")})
                continue
            # Final answers are already rendered in the transcript.
            if method == "turn/completed" or (isinstance(item, dict) and item.get("type") == "agentMessage"
                                              and item.get("phase") in (None, "final_answer")):
                continue
            if method in ("item/started", "item/completed", "turn/completed"):
                item = params.get("item") or {}
                if not isinstance(item, dict):
                    item = {}
                public_item = {key: item[key] for key in ("id", "type", "phase") if key in item}
                if item.get("type") == "mcpToolCall":
                    for key in ("server", "tool", "status"):
                        if isinstance(item.get(key), str):
                            public_item[key] = item[key][:120]
                    if item.get("server") == "local_agents" and item.get("tool") == "delegate_task":
                        child = _delegate_receipt(item.get("result"))
                        if child:
                            public_item["child"] = child
                if isinstance(item.get("command"), str):
                    public_item["command"] = item["command"][:800]
                if isinstance(item.get("text"), str):
                    # Commentary is rendered as a live assistant message and is
                    # persisted with the completed turn.  The former shared
                    # 800-character preview limit visibly cut normal updates.
                    public_item["text"] = item["text"][:16000]
                if isinstance(item.get("changes"), list):
                    public_item["changes"] = [{"path": str(change.get("path") or "")[:300]}
                                              for change in item["changes"][:20] if isinstance(change, dict)]
                turn = params.get("turn") or {}
                if not isinstance(turn, dict):
                    turn = {}
                public_events.append({"method": method, "item": public_item or None,
                                      "turn": {key: turn[key] for key in ("id", "status") if key in turn} or None,
                                      "received_at": event.get("_received_at")})
        for approval in self.approvals.values():
            if approval["thread_id"] != thread_id:
                continue
            item_id = approval["params"].get("itemId")
            preview = next(((e.get("params") or {}).get("item") for e in reversed(events)
                            if e.get("method") == "item/started"
                            and ((e.get("params") or {}).get("item") or {}).get("id") == item_id), None)
            pending.append({**approval, "preview": preview})
        return {"events": public_events, "approvals": pending, "active": active,
                "context": self.token_usage.get(thread_id),
                "context_pending": bool(self.context_pending.get(thread_id)),
                "rate_limits": self.rate_limits or None}

    async def steer(self, thread_id: str, prompt: str) -> str:
        turn_id = self.active_turns.get(thread_id)
        if not turn_id:
            raise CodexBridgeError("Codex no longer has an active turn to steer")
        params = {"threadId": thread_id, "expectedTurnId": turn_id,
                  "input": [{"type": "text", "text": prompt}]}
        try:
            await self._call("turn/steer", params)
        except CodexBridgeError as exc:
            actual_turn_id = _reported_active_turn(exc)
            if not actual_turn_id or actual_turn_id == turn_id:
                raise
            # app-server is the source of truth. This can happen when a steer
            # advances/replaces a turn before the dashboard receives its event.
            self.active_turns[thread_id] = actual_turn_id
            turn_id = actual_turn_id
            params["expectedTurnId"] = turn_id
            await self._call("turn/steer", params)
        return turn_id

    async def interrupt(self, thread_id: str) -> str:
        turn_id = self.active_turns.get(thread_id)
        if not turn_id:
            raise CodexBridgeError("Codex no longer has an active turn to stop")
        await self._call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return turn_id

    async def decide(self, thread_id: str, request_id: str, decision: str) -> None:
        if decision not in ("accept", "decline"):
            raise CodexBridgeError("Invalid approval decision")
        approval = self.approvals.get(request_id)
        if approval is None or approval["thread_id"] != thread_id:
            raise CodexBridgeError("Approval is no longer pending")
        await self._write({"id": int(request_id), "result": {"decision": decision}})
        self.approvals.pop(request_id, None)

    async def turn(self, thread_id: str | None, prompt: str, cwd: str, on_thread=None,
                   images: list[dict] | None = None, model: str | None = None,
                   effort: str | None = None, reviewer: str = "user",
                   mode: str = "coding", access: str = "local",
                   conversation_id: str | None = None, surface: str = "local") -> tuple[str, str]:
        if mode not in ("chat", "coding"):
            raise CodexBridgeError("Invalid Codex conversation mode")
        workspace = self.research_workspace(cwd) if mode == "chat" else self.workspace(cwd)
        if reviewer not in ("user", "auto_review"):
            raise CodexBridgeError("Invalid Codex reviewer")
        if access not in ("local", "workspace"):
            raise CodexBridgeError("Invalid Codex access level")
        if surface not in ("local", "mobile"):
            raise CodexBridgeError("Invalid dashboard surface")
        permission = ":danger-full-access" if access == "local" else ":workspace"
        try:
            tool_kwargs = {"workspace": workspace if mode == "coding" else None,
                           "mode": mode, "conversation_id": conversation_id}
            if surface == "mobile":
                tool_kwargs["surface"] = surface
            tool_config = dashboard_mcp_servers.cloud_config(**tool_kwargs)
        except ValueError as exc:
            raise CodexBridgeError(str(exc)) from exc
        config = {"approvals_reviewer": reviewer if mode == "coding" else "user",
                  "web_search": "disabled"}
        if tool_config["mcp_servers"]:
            config.update(tool_config)
        # Conversation creation already checks authentication. Avoid placing a
        # second account/read behind another active app-server turn.
        if thread_id:
            result = await self._call("thread/resume", {"threadId": thread_id, "config": config})
        else:
            start_params = {
                "cwd": str(workspace), "approvalPolicy": "never" if access == "local" else "on-request",
                "serviceName": "llm_gateway_chat", "config": config,
                "permissions": permission,
            }
            if model:
                start_params["model"] = model
            result = await self._call("thread/start", start_params)
        thread_id = result["thread"]["id"]
        if on_thread:
            await on_thread(thread_id)
        queue = self.events.setdefault(thread_id, asyncio.Queue())
        # Completed turn activity is stored with the transcript by ChatStore.
        # Keep this deque scoped to the current turn to avoid duplicate receipts.
        self.recent[thread_id] = deque()
        answer = []
        try:
            inputs = [{"type": "text", "text": prompt}]
            inputs.extend({"type": "image", "url": image["data_url"]} for image in (images or []))
            turn_params = {"threadId": thread_id,
                "input": inputs,
                "cwd": str(workspace), "approvalPolicy": "never" if access == "local" else "on-request",
                "permissions": permission,
            }
            if model:
                turn_params["model"] = model
            if effort:
                turn_params["effort"] = effort
            started = await self._call("turn/start", turn_params)
            turn_id = started["turn"]["id"]
            self.active_turns[thread_id] = turn_id
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 3600)
                except asyncio.TimeoutError as exc:
                    raise CodexBridgeError("Codex turn had no activity for one hour") from exc
                method, params = event.get("method"), event.get("params") or {}
                if method == "bridge/exited":
                    raise CodexAppServerExited("Codex app-server exited during the turn")
                # A concurrent steer may discover that app-server has already
                # advanced this thread to another turn. Follow the corrected
                # ID so its final answer is not silently discarded.
                current_turn_id = self.active_turns.get(thread_id, turn_id)
                if params.get("turnId") not in (None, current_turn_id):
                    continue
                if method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
                    text = params["item"].get("text")
                    if isinstance(text, str):
                        answer.append((params["item"].get("phase"), text))
                if method == "turn/completed" and (params.get("turn") or {}).get("id") == current_turn_id:
                    turn = params["turn"]
                    if turn.get("status") != "completed":
                        error = turn.get("error")
                        message = str(error or "Codex turn did not complete")
                        if turn.get("status") in ("interrupted", "cancelled", "canceled"):
                            raise CodexTurnInterrupted(message)
                        if isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded":
                            raise CodexUsageLimitError(str(error.get("message") or message), error)
                        raise CodexBridgeError(message)
                    final = [text for phase, text in answer if phase == "final_answer" and text.strip()]
                    content = "\n\n".join(final or [text for _, text in answer if text.strip()]).strip()
                    if not content:
                        raise CodexBridgeError("Codex returned no message")
                    return thread_id, content
        finally:
            self.active_turns.pop(thread_id, None)
            self.events.pop(thread_id, None)
