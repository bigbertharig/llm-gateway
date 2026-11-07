"""What the dashboard shows beyond the rig manager's own status (docs/DASHBOARD.md).

- models: the summary in the gateway's `model_catalog.json`, which the importer (tools/generate_profiles.py)
  builds from the benchmarking registry and roles, the best result per model and key test from
  `results/model_benchmark_records.jsonl` (limit-50 runs over smoke runs), and the latest runtime
  characterization per model (`build_models_summary`); the dashboard itself never reads benchmarking;
- benchmarks: lane runs (`logs/benchmarks/worker_lanes_*`) and campaign runs (`campaigns/history`) on the
  shared drive;
- orchestration: llm_orchestration's versioned private API over a Unix socket.

All readers are plain functions over files (blocking): the routes run them in a thread.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ..manager import models as registry_models

SHARED_ROOTS = (Path("/media/bryan/shared"), Path("/mnt/shared"))
ORCHESTRATION_API_SOCKET = Path("/run/llm-orchestration/api.sock")

# The tests shown per model, in column order: (label, test_id).
# Reasoning columns are the lenient rows (bench-reasoning extract v5): a correct answer counts however it
# is written. IFEval stays strict because its prompts instruct the format.
KEY_TESTS = (
    ("bbh", "bbh_flexible"),
    ("gsm8k", "gsm8k_flexible"),
    ("drop", "drop_flexible"),
    ("ifeval", "ifeval_prompt_level_strict_acc_none"),
    ("mmlu-pro", "mmlu_pro_flexible"),
    ("humaneval+", "humaneval_plus"),
    ("mbpp+", "mbpp_plus"),
)
# Which run counts when a model has several: a limit-50 or full run over a smoke run, then the newer
# extractor (a re-scored row keeps its run's time), then the newest.
CLASS_RANK = {"validated": 3, "full": 3, "smoke": 1}
EXTRACT_VERSION = re.compile(r"extract-v(\d+)$")
LANE_ACTIVE_S = 1200     # a lane log written to within this long counts as running
RECENT_RUNS = 12


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

_records_cache: dict[str, Any] = {"key": None, "best": {}}


def _best_records(path: Path) -> dict[tuple[str, str], dict]:
    """(model, test_id) -> the record that counts, cached until the file changes."""
    try:
        key = (str(path), path.stat().st_mtime, path.stat().st_size)
    except OSError:
        return {}
    if _records_cache["key"] == key:
        return _records_cache["best"]
    best: dict[tuple[str, str], dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m, t = r.get("model"), r.get("test_id")
            if not m or not t or r.get("score") is None:
                continue
            version = EXTRACT_VERSION.search(str((r.get("runtime") or {}).get("config_id") or ""))
            rank = (CLASS_RANK.get(str(r.get("run_class") or ""), 0), int(version.group(1)) if version else 0,
                    str(r.get("run_at") or ""))
            have = best.get((m, t))
            if have is None or rank > have["_rank"]:
                best[(m, t)] = {"score": float(r["score"]), "run_class": r.get("run_class"),
                                "n": r.get("sample_count"), "at": str(r.get("run_at") or "")[:10], "_rank": rank,
                                "unreadable": (r.get("extractor_failure_count") or 0) if version and int(version.group(1)) >= 5 else None}
    _records_cache.update(key=key, best=best)
    return best


def _slug(model: str) -> str:
    return re.sub(r"[:.]", "_", model)


def _runtime(results: Path, model: str) -> dict | None:
    files = sorted((results / "runtime_experiments").glob(f"{_slug(model)}_runtime_characterization_*.json"))
    if not files:
        return None
    d = _json(files[-1]) or {}
    rt, dec = d.get("runtime") or {}, d.get("decode") or {}
    return {"decode_tps": dec.get("generation_tokens_per_second"), "prompt_tps": dec.get("prompt_tokens_per_second"),
            "ctx": rt.get("ctx_size"), "tensor_split": rt.get("tensor_split"),
            "file": files[-1].name, "at": files[-1].stem.rsplit("_", 1)[-1]}


def models_summary() -> dict:
    """The model summary the importer copied into model_catalog.json (tools/generate_profiles.py)."""
    try:
        return registry_models.catalog().get("summary") or {"models": [], "roles": {}, "key_tests": []}
    except (OSError, ValueError):
        return {"models": [], "roles": {}, "key_tests": [], "error": "model_catalog.json missing: run the importer"}


def build_models_summary(root: Path) -> dict:
    """Import time only (tools/generate_profiles.py): the summary from a benchmarking checkout."""
    registry = (_json(root / "model_registry.json") or {}).get("models", {})
    roles = (_json(root / "model_roles.json") or {}).get("roles", {})
    best = _best_records(root / "results" / "model_benchmark_records.jsonl")
    rows = []
    for mid, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        sched = entry.get("schedule") or {}
        scores = {}
        for label, test in KEY_TESTS:
            r = best.get((mid, test))
            if r:
                scores[label] = {k: v for k, v in r.items() if not k.startswith("_")}
        pipeline = [best[(mid, t)]["score"] for (m, t) in best if m == mid and t.startswith("custom_")]
        rows.append({
            "model": mid, "status": entry.get("status"), "placement": sched.get("placement"),
            "tier": sched.get("tier"), "profiles": sorted((entry.get("profiles") or {}).keys()),
            "roles": sorted(name for name, spec in roles.items() if isinstance(spec, dict) and spec.get("model") == mid),
            "source_url": entry.get("source_url"), "superseded_by": entry.get("superseded_by"),
            "scores": scores, "pipeline": round(sum(pipeline) / len(pipeline), 3) if pipeline else None,
            "pipeline_tests": len(pipeline), "runtime": _runtime(root / "results", mid),
        })
    order = {"active": 0, "candidate": 1}
    rows.sort(key=lambda r: (order.get(r["status"], 2), r["model"]))
    return {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "key_tests": [l for l, _ in KEY_TESTS],
            "models": rows, "roles": {k: v.get("model") for k, v in roles.items() if isinstance(v, dict)}}


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
    for run in sorted(logs.glob("worker_lanes_*"), key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT_RUNS]:
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
            lanes.append({"run": run.name, "lane": "gpu " + group, "last": last[-240:], "age_s": age,
                          "active": active, "queued": queued})
    campaigns = []
    statuses = sorted((logs / "campaigns" / "history").glob("*/*/status.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:RECENT_RUNS]
    for path in statuses:
        d = _json(path) or {}
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
                          "updated_age_s": int(now - path.stat().st_mtime)})
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


def orchestration_options() -> dict:
    return orchestration_request("GET", "/v1/orchestration/options")


def orchestration_action(action: str, body: dict) -> dict:
    return orchestration_request("POST", f"/v1/orchestration/actions/{action}", body)
