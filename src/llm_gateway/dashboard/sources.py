"""What the dashboard shows beyond the rig manager's own status (docs/DASHBOARD.md).

- models: the summary in the gateway's `model_catalog.json`, which the importer (tools/generate_profiles.py)
  imports from benchmarking's versioned model-library export; the dashboard itself never reads benchmarking;
- benchmarks: lane runs (`logs/benchmarks/worker_lanes_*`) and campaign runs (`campaigns/history`) on the
  shared drive;
- orchestration: llm_orchestration's versioned private API over a Unix socket.

All readers are plain functions over files (blocking): the routes run them in a thread.
"""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

from ..manager import models as registry_models

SHARED_ROOTS = (Path("/media/bryan/shared"), Path("/mnt/shared"))
ORCHESTRATION_API_SOCKET = Path("/run/llm-orchestration/api.sock")

LANE_ACTIVE_S = 1200     # a lane log written to within this long counts as running
RECENT_RUNS = 12
RECENT_HISTORY_S = 24 * 60 * 60


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


# ------------------------------------------------------------------ benchmarks

def _last_line(path: Path) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 4096))
            lines = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def benchmarks_summary(shared: Path | None = None, claims: set[str] | None = None) -> dict:
    """Lane and campaign runs. A lane is running while it holds its claim (`lane-<cards>`, taken at
    start and released at LANE_DONE); its log is written only at phase changes, so log age says little.
    Only the newest run per card group can be the running one. Without `claims`, log age decides."""
    logs = (shared or shared_root()) / "logs" / "benchmarks"
    now = time.time()
    lanes = []
    seen_groups: set[str] = set()
    recent_lane_count = 0
    for run in sorted(logs.glob("worker_lanes_*"), key=lambda p: p.stat().st_mtime, reverse=True):
        queue = run / "queue.txt"
        queued = [q for q in queue.read_text(encoding="utf-8").splitlines() if q.strip()] if queue.exists() else []
        for log in sorted(run.glob("lane_gpu*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
            last = _last_line(log)
            age = int(now - log.stat().st_mtime)
            group = log.stem.replace("lane_gpu", "")
            newest = group not in seen_groups
            seen_groups.add(group)
            if claims is not None:
                active = newest and f"lane-{group}" in claims and "LANE_DONE" not in last
            else:
                active = newest and age < LANE_ACTIVE_S and "LANE_DONE" not in last
            if not active and (age > RECENT_HISTORY_S or recent_lane_count >= RECENT_RUNS):
                continue
            if not active:
                recent_lane_count += 1
            lanes.append({"run": run.name, "lane": "gpu " + group, "last": last[-240:], "age_s": age,
                          "active": active, "queued": queued})
    campaigns = []
    statuses = sorted((logs / "campaigns" / "history").glob("*/*/status.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    recent_campaign_count = 0
    for path in statuses:
        d = _json(path) or {}
        age = int(now - path.stat().st_mtime)
        # A status left as "running" after a crash is not evidence of a live job.
        # Active campaigns refresh this receipt, including jobs started days ago.
        if age > RECENT_HISTORY_S or recent_campaign_count >= RECENT_RUNS:
            continue
        recent_campaign_count += 1
        blocks = d.get("blocks") or {}
        counts: dict[str, int] = {}
        current = []
        for bid, b in blocks.items():
            st = str((b or {}).get("status", "?"))
            counts[st] = counts.get(st, 0) + 1
            if st not in ("completed", "failed", "pending", "waiting_deps", "waiting_gpu"):
                current.append(f"{bid} ({st})")
        campaigns.append({"campaign": d.get("campaign") or path.parent.parent.name, "run_id": d.get("run_id"),
                          "status": d.get("status"), "blocks": counts, "current": current,
                          "updated_age_s": age})
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "lanes": lanes, "campaigns": campaigns}


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
