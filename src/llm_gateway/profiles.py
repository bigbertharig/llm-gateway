from __future__ import annotations

import json
import threading
from pathlib import Path


class ProfileError(RuntimeError):
    pass


class Profiles:
    """remote_profiles from model_task_library.json, re-read whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._profiles: dict[str, dict] = {}

    def all(self) -> dict[str, dict]:
        with self._lock:
            try:
                mtime = self.path.stat().st_mtime
            except OSError as exc:
                raise ProfileError(f"cannot read {self.path}: {exc}") from exc
            if mtime != self._mtime:
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ProfileError(f"cannot parse {self.path}: {exc}") from exc
                items = payload.get("remote_profiles")
                if not isinstance(items, list) or not items:
                    raise ProfileError(f"no remote_profiles in {self.path}")
                self._profiles = {str(p["id"]): p for p in items}
                self._mtime = mtime
            return dict(self._profiles)
