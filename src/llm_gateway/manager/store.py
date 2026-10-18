"""Persistent manager state: GPU leases and operator claims.

Leases use the orchestration repo's lease store (`shared/agents/gpu_leases.py`, file
`shared/gpus/leases.json`) unchanged, so GPU workers and the brain keep reading the same file. The
manager is its only writer. Claims are the manager's own (`shared/gpus/claims.json`): a claim takes
cards out of the shared pool; only lease requests that name the claim can use them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any


class ClaimError(ValueError):
    pass


def load_lease_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("rig_gpu_leases", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load lease store from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ClaimStore:
    """{"schema_version": 1, "claims": [{name, gpu_ids, note, by, at}]}; written atomically."""

    def __init__(self, shared_path: Path):
        self.path = shared_path / "gpus" / "claims.json"
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return list(json.loads(self.path.read_text(encoding="utf-8")).get("claims", []))

    def _write(self, claims: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps({"schema_version": 1, "claims": claims}, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def add(self, name: str, gpu_ids: list[int], note: str = "", by: str = "") -> dict[str, Any]:
        name = str(name or "").strip()
        if not name:
            raise ClaimError("claim name is required")
        ids = sorted({int(g) for g in gpu_ids})
        if not ids:
            raise ClaimError("a claim needs at least one GPU")
        with self._lock:
            claims = self.list()
            if any(c["name"] == name for c in claims):
                raise ClaimError(f"claim {name} already exists")
            for c in claims:
                overlap = sorted(set(ids) & set(c["gpu_ids"]))
                if overlap:
                    raise ClaimError(f"GPU {overlap} already claimed by {c['name']}")
            claim = {"name": name, "gpu_ids": ids, "note": note, "by": by, "at": _now()}
            self._write(claims + [claim])
            return claim

    def remove(self, name: str) -> dict[str, Any]:
        with self._lock:
            claims = self.list()
            match = [c for c in claims if c["name"] == name]
            if not match:
                raise KeyError(f"no claim {name}")
            self._write([c for c in claims if c["name"] != name])
            return match[0]
