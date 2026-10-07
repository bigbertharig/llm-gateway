from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass

# Exit codes of the rig mode selector (shared/scripts/rig/rig.py).
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


@dataclass
class _Inflight:
    done: threading.Event
    error: RigUnavailable | None = None


class Rig:
    def __init__(self, command: tuple[str, ...], start_timeout_seconds: float):
        self.command = command
        self.start_timeout_seconds = start_timeout_seconds
        self._lock = threading.Lock()
        self._inflight: dict[str, _Inflight] = {}

    def _run(self, *args: str, timeout: float) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [*self.command, *args], capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise RigUnavailable(504, f"rig {' '.join(args)} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise RigUnavailable(502, f"cannot run rig command {self.command}: {exc}") from exc

    def status(self) -> dict:
        proc = self._run("status", "--json", timeout=30)
        if proc.returncode != RIG_OK:
            raise RigUnavailable(502, f"rig status failed: {(proc.stdout + proc.stderr).strip()}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RigUnavailable(502, f"rig status returned invalid JSON: {proc.stdout[:200]}") from exc

    def ready_port(self, profile_id: str) -> int | None:
        for p in self.status().get("profiles", []):
            if p.get("id") == profile_id and p.get("state") == "ready":
                return int(p["port"])
        return None

    def ensure_ready(self, profile_id: str) -> int:
        """Return the port serving profile_id, loading it on the rig first if needed."""
        port = self.ready_port(profile_id)
        if port is not None:
            return port
        with self._lock:
            flight = self._inflight.get(profile_id)
            leader = flight is None
            if leader:
                flight = _Inflight(done=threading.Event())
                self._inflight[profile_id] = flight
        if leader:
            try:
                self._start(profile_id)
            except RigUnavailable as exc:
                flight.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(profile_id, None)
                flight.done.set()
        else:
            flight.done.wait(self.start_timeout_seconds)
        if flight.error:
            raise flight.error
        port = self.ready_port(profile_id)
        if port is None:
            raise RigUnavailable(502, f"rig reported {profile_id} started but it is not ready")
        return port

    def _start(self, profile_id: str) -> None:
        deadline = time.monotonic() + self.start_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RigUnavailable(504, f"{profile_id} not ready within {self.start_timeout_seconds:.0f}s")
            proc = self._run("start", "remote", profile_id, timeout=remaining)
            out = (proc.stdout + proc.stderr).strip()
            if proc.returncode == RIG_OK:
                return
            if proc.returncode == RIG_LOCKED:
                time.sleep(LOCKED_RETRY_SECONDS)
                continue
            if proc.returncode == RIG_BUSY:
                raise RigUnavailable(503, f"rig busy: {out.splitlines()[0] if out else 'another mode'}")
            if proc.returncode == RIG_NO_CAPACITY:
                raise RigUnavailable(503, out)
            raise RigUnavailable(502, f"rig failed to start {profile_id}: {out[-1500:]}")
