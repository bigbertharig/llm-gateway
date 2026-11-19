"""Gateway-owned dashboard reads and the orchestration private API adapter."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
from typing import Any

from ..manager import models as registry_models

ORCHESTRATION_API_SOCKET = Path("/run/llm-orchestration/api.sock")

SHARED_ROOTS = (Path("/media/bryan/shared"), Path("/mnt/shared"))  # gateway-owned plan source


def shared_root() -> Path:
    env = os.getenv("SHARED_ROOT")
    for root in [Path(env)] if env else SHARED_ROOTS:
        if root.is_dir():
            return root
    raise FileNotFoundError("shared drive not found; set SHARED_ROOT")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ models

def models_summary() -> dict:
    """The model summary the importer copied into model_catalog.json (tools/generate_profiles.py)."""
    try:
        return registry_models.catalog().get("summary") or {"models": [], "roles": {}, "key_tests": []}
    except (OSError, ValueError):
        return {"models": [], "roles": {}, "key_tests": [], "error": "model_catalog.json missing: run the importer"}


def benchmark_evidence_summary() -> dict:
    """Read published model evidence from this repo's imported catalog snapshot."""
    summary = models_summary()
    models = summary.get("models") or []
    return {"source": "gateway_model_catalog", "models": [
        {"model": m.get("model"), "status": m.get("status"), "scores": m.get("scores") or {},
         "runtime": m.get("runtime") or {}, "profiles": m.get("profiles") or []}
        for m in models], "key_tests": summary.get("key_tests") or [], "error": summary.get("error")}


# ------------------------------------------------------------------ orchestration

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        import socket
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def orchestration_request(method: str, path: str, body: dict | None = None) -> dict:
    socket_path = os.getenv("ORCHESTRATION_API_SOCKET", str(ORCHESTRATION_API_SOCKET))
    connection = _UnixConnection(socket_path)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        if response.status != 200:
            raise RuntimeError(result.get("error", f"orchestration API HTTP {response.status}"))
        return result
    finally:
        connection.close()


def orchestration_summary() -> dict:
    return orchestration_request("GET", "/v1/orchestration/status")


def orchestration_diagnostics() -> dict:
    return orchestration_request("GET", "/v1/orchestration/diagnostics")


def orchestration_activity(batch_id: str, after: int = 0) -> dict:
    from urllib.parse import urlencode
    return orchestration_request("GET", "/v1/orchestration/activity?" +
                                 urlencode({"batch_id": batch_id, "after": after}))


def orchestration_options() -> dict:
    from .plan_requests import PlanRequests
    return PlanRequests(shared_root(), {}).options()


def orchestration_action(action: str, body: dict) -> dict:
    return orchestration_request("POST", f"/v1/orchestration/actions/{action}", body)
