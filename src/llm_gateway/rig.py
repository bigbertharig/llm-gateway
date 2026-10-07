from __future__ import annotations

import asyncio
import json
import time

# Exit codes of the rig mode selector (llm_orchestration shared/scripts/rig/rig.py).
RIG_OK = 0
RIG_FAILED = 1
RIG_BUSY = 2
RIG_LOCKED = 3
RIG_NO_CAPACITY = 4

LOCKED_RETRY_SECONDS = 3.0


class RigUnavailable(RuntimeError):
    """The rig cannot serve the profile right now. Carries the HTTP status to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Rig:
    """Async wrapper around `rig status --json` / `rig start remote <id>`."""

    def __init__(self, command: tuple[str, ...], start_timeout_seconds: float):
        self.command = command
        self.start_timeout_seconds = start_timeout_seconds
        self._inflight: dict[str, asyncio.Task] = {}

    async def _run(self, *args: str, timeout: float) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise RigUnavailable(502, f"cannot run rig command {self.command}: {exc}") from exc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RigUnavailable(504, f"rig {' '.join(args)} timed out after {timeout:.0f}s") from exc
        return proc.returncode, out.decode("utf-8", "replace").strip()

    async def status(self) -> dict:
        code, out = await self._run("status", "--json", timeout=30)
        if code != RIG_OK:
            raise RigUnavailable(502, f"rig status failed: {out}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise RigUnavailable(502, f"rig status returned invalid JSON: {out[:200]}") from exc

    async def ready_port(self, profile_id: str) -> int | None:
        for p in (await self.status()).get("profiles", []):
            if p.get("id") == profile_id and p.get("state") == "ready":
                return int(p["port"])
        return None

    async def ensure_ready(self, profile_id: str) -> int:
        """Return the port serving profile_id, loading it on the rig first if needed.

        Concurrent callers share one start. The start is shielded: a client that disconnects
        doesn't cancel a load other requests (or its own retry) are waiting on.
        """
        port = await self.ready_port(profile_id)
        if port is not None:
            return port
        task = self._inflight.get(profile_id)
        if task is None:
            task = asyncio.create_task(self._start(profile_id))
            self._inflight[profile_id] = task
            task.add_done_callback(lambda _t: self._inflight.pop(profile_id, None))
        await asyncio.shield(task)
        port = await self.ready_port(profile_id)
        if port is None:
            raise RigUnavailable(502, f"rig reported {profile_id} started but it is not ready")
        return port

    async def _start(self, profile_id: str) -> None:
        deadline = time.monotonic() + self.start_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RigUnavailable(504, f"{profile_id} not ready within {self.start_timeout_seconds:.0f}s")
            code, out = await self._run("start", "remote", profile_id, timeout=remaining)
            if code == RIG_OK:
                return
            if code == RIG_LOCKED:
                await asyncio.sleep(LOCKED_RETRY_SECONDS)
                continue
            if code == RIG_BUSY:
                raise RigUnavailable(503, f"rig busy: {out.splitlines()[0] if out else 'another mode'}")
            if code == RIG_NO_CAPACITY:
                raise RigUnavailable(503, out)
            raise RigUnavailable(502, f"rig failed to start {profile_id}: {out[-1500:]}")
