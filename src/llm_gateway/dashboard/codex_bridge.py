"""Private stdio bridge to Codex app-server for dashboard cloud conversations.

The process runs as the gateway service user on the Pi. Its authentication store
never crosses the dashboard API; only account display state and a device code do.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


class CodexBridgeError(RuntimeError):
    pass


class CodexBridge:
    def __init__(self, command: str | None = None):
        self.command = command or os.getenv("CODEX_CHAT_COMMAND", "codex")
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: dict[str, asyncio.Queue] = {}
        self.recent: dict[str, deque] = {}
        self.approvals: dict[str, dict] = {}
        self.login_event: dict | None = None
        self.account_state: dict | None = None
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.workspaces = [Path(p).resolve() for p in os.getenv(
            "CODEX_CHAT_WORKSPACES", "/home/bryan/llm-gateway:/home/bryan/llm_orchestration:/home/bryan/llm-harness"
        ).split(":") if p]

    def workspace(self, path: str) -> Path:
        selected = Path(path).resolve()
        if selected not in self.workspaces or not selected.is_dir():
            raise CodexBridgeError("Choose an available coding workspace")
        return selected

    async def start(self) -> None:
        async with self.start_lock:
            if self.process and self.process.returncode is None:
                return
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.command, "app-server", "--listen", "stdio://",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                raise CodexBridgeError("Codex app-server is not installed on the gateway host") from exc
            self.reader_task = asyncio.create_task(self._read())
            await self._call("initialize", {"clientInfo": {
                "name": "llm_gateway_chat", "title": "Rig dashboard chat", "version": "0.1.0",
            }}, ensure_started=False)
            await self._write({"method": "initialized", "params": {}})

    async def close(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader_task:
            self.reader_task.cancel()
        self.process = None

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
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                message["_received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if "method" not in message and "id" in message and message["id"] in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        future.set_result(message)
                    continue
                method, params = message.get("method"), message.get("params") or {}
                if method == "account/login/completed":
                    self.login_event = params
                    self.account_state = None
                thread_id = params.get("threadId")
                if thread_id in self.events:
                    self.events[thread_id].put_nowait(message)
                if thread_id:
                    self.recent.setdefault(thread_id, deque(maxlen=80)).append(message)
                if "id" in message and method:
                    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                        self.approvals[str(message["id"])] = {"id": str(message["id"]),
                            "method": method, "thread_id": thread_id, "params": params}
                    else:
                        await self._write({"id": message["id"], "error": {
                            "code": -32601, "message": "This dashboard does not support the requested interaction"}})
                if method == "serverRequest/resolved":
                    self.approvals.pop(str(params.get("requestId")), None)
        finally:
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

    async def compact(self, thread_id: str) -> None:
        await self._call("thread/compact/start", {"threadId": thread_id})

    async def models(self) -> list[dict]:
        result = await self._call("model/list", {"includeHidden": False, "limit": 100})
        return [item for item in result.get("data", []) if not item.get("hidden")]

    def status(self, thread_id: str) -> dict:
        active = thread_id in self.events
        events = self.recent.get(thread_id, ())
        public_events = []
        pending = []
        for event in list(events):
            method, params = event.get("method"), event.get("params") or {}
            item = params.get("item") or {}
            # Final answers are already rendered in the transcript.
            if method == "turn/completed" or (method == "item/completed" and isinstance(item, dict) and item.get("type") == "agentMessage"):
                continue
            if method in ("item/started", "item/completed", "turn/completed"):
                item = params.get("item") or {}
                if not isinstance(item, dict):
                    item = {}
                public_item = {key: item[key] for key in ("id", "type", "phase") if key in item}
                for key in ("command", "text"):
                    if isinstance(item.get(key), str):
                        public_item[key] = item[key][:800]
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
        return {"events": public_events, "approvals": pending, "active": active}

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
                   effort: str | None = None, reviewer: str = "user") -> tuple[str, str]:
        workspace = self.workspace(cwd)
        if reviewer not in ("user", "auto_review"):
            raise CodexBridgeError("Invalid Codex reviewer")
        config = {"approvals_reviewer": reviewer}
        # Conversation creation already checks authentication. Avoid placing a
        # second account/read behind another active app-server turn.
        if thread_id:
            result = await self._call("thread/resume", {"threadId": thread_id, "config": config})
        else:
            start_params = {
                "cwd": str(workspace), "approvalPolicy": "on-request", "sandbox": "workspace-write",
                "serviceName": "llm_gateway_chat", "config": config,
            }
            if model:
                start_params["model"] = model
            result = await self._call("thread/start", start_params)
        thread_id = result["thread"]["id"]
        if on_thread:
            await on_thread(thread_id)
        queue = self.events.setdefault(thread_id, asyncio.Queue())
        self.recent[thread_id] = deque(maxlen=80)
        answer = []
        try:
            inputs = [{"type": "text", "text": prompt}]
            inputs.extend({"type": "image", "url": image["data_url"]} for image in (images or []))
            turn_params = {"threadId": thread_id,
                "input": inputs,
                "cwd": str(workspace), "approvalPolicy": "on-request",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(workspace)],
                                  "networkAccess": False},
            }
            if model:
                turn_params["model"] = model
            if effort:
                turn_params["effort"] = effort
            started = await self._call("turn/start", turn_params)
            turn_id = started["turn"]["id"]
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 3600)
                except asyncio.TimeoutError as exc:
                    raise CodexBridgeError("Codex turn had no activity for one hour") from exc
                method, params = event.get("method"), event.get("params") or {}
                if method == "bridge/exited":
                    raise CodexBridgeError("Codex app-server exited during the turn")
                if params.get("turnId") not in (None, turn_id):
                    continue
                if method == "item/completed" and (params.get("item") or {}).get("type") == "agentMessage":
                    text = params["item"].get("text")
                    if isinstance(text, str):
                        answer.append((params["item"].get("phase"), text))
                if method == "turn/completed" and (params.get("turn") or {}).get("id") == turn_id:
                    turn = params["turn"]
                    if turn.get("status") != "completed":
                        raise CodexBridgeError(str(turn.get("error") or "Codex turn did not complete"))
                    final = [text for phase, text in answer if phase == "final_answer" and text.strip()]
                    content = "\n\n".join(final or [text for _, text in answer if text.strip()]).strip()
                    if not content:
                        raise CodexBridgeError("Codex returned no message")
                    return thread_id, content
        finally:
            self.events.pop(thread_id, None)
