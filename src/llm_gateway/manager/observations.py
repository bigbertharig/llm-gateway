"""What the manager has seen the rig do, kept in benchmarking so it is learned from across sessions.

Benchmarking holds the rig's history (registry, scores, capacity and runtime records); the manager reads it to
plan and writes back here what only it sees: every load it places, as one row in
`<benchmarking>/results/load_observations.jsonl`, committed with the other results. Kinds:

- `load`: a ready load. Model, GGUF, micro-batch, use, the cards by UUID with their link and root port (the
  rig's scan), the manager's estimate per card, what llama.cpp allocated per card (the rig's `fit`), memory in
  use per card once serving (nvidia-smi), and the load time. `fit.compute_mb` and `fit.overhead_mb` plan from
  these and from the capacity records.
- `load_failed`: a load the rig refused or that never served, with the same facts, the error and whether it ran
  out of memory: the other side of the fit boundary (`fit.failed_ceiling`).

Rows without `kind` are loads. Runtime coordination (the manager's requests, the orchestration's handling) is
not kept here, only what a later session should know.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import models

PATH: str | None = None  # override (tests); else OBSERVATIONS_PATH, else the benchmarking results folder
FILE = "load_observations.jsonl"
KINDS = ("load", "load_failed")
CARD_KEYS = ("index", "uuid", "name", "total_mb", "bus", "link_gen", "link_width", "root_port", "via")


def results_dir() -> Path | None:
    try:
        return models.benchmarking_root() / "results"
    except FileNotFoundError:
        return None


def path() -> str:
    if PATH:
        return PATH
    if os.getenv("OBSERVATIONS_PATH"):
        return os.environ["OBSERVATIONS_PATH"]
    d = results_dir()
    if d is None:
        raise OSError("benchmarking results folder not found; set BENCHMARKING_ROOT or OBSERVATIONS_PATH")
    return str(d / FILE)


def record(kind: str, **fields: Any) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown observation kind {kind!r}; one of {KINDS}")
    row = {"kind": kind, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields}
    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return row


def rows(kind: str | None = None, model: str | None = None) -> Iterator[dict]:
    try:
        f = open(path(), encoding="utf-8")
    except OSError:
        return
    with f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            row.setdefault("kind", "load")
            if (kind is None or row["kind"] == kind) and (model is None or row.get("model") == model):
                yield row


def capacity_loads() -> Iterator[dict]:
    """The loads inside benchmarking's capacity records (`results/runtime_experiments/*_capacity_*.json`), as
    load rows: read where they live, so history from before this log, and from tools that load by themselves,
    counts too. used_mb is the peak memory a probe saw per card (nvidia-smi), so overhead from these is an upper
    bound. Records whose model is no longer in the registry are skipped."""
    d = results_dir()
    if d is None:
        return
    for f in sorted((d / "runtime_experiments").glob("*_capacity_*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            measured = (rec.get("load") or {}).get("measured") or {}
            if not measured.get("cards") or not measured.get("n_ctx"):
                continue
            r = models.resolve(rec["model"], "chat", "default")
        except (OSError, ValueError, KeyError, FileNotFoundError):
            continue
        gpus = [int(g) for g in rec.get("cards") or []]
        peak: dict[int, float] = {}
        for p in [rec.get("short_probe") or {}, *(rec.get("depth_probes") or [])]:
            for e in p.get("gpu_envelope") or []:
                peak[int(e["gpu"])] = max(peak.get(int(e["gpu"]), 0.0), float(e.get("peak_memory_mb") or 0))
        sizes = {int(c["gpu"]): c.get("total_mb") for c in (rec.get("plan") or {}).get("cards") or []}
        yield {"kind": "load", "from_record": f.name, "model": rec["model"],
               "source": os.path.basename(r["gguf_path"]),
               "ubatch": min(int((r.get("runtime") or {}).get("batch_size", 64)), 512),
               "n_ctx": int(measured["n_ctx"]), "gpus": gpus, "cards": measured["cards"],
               "hw": [{"index": g, "total_mb": sizes.get(g)} for g in gpus],
               "used_mb": [peak.get(g) for g in gpus]}


def history(kind: str = "load") -> Iterator[dict]:
    """Every known load: this log, then the capacity records, the same load counted once."""
    seen = set()
    for row in [*rows(kind), *(capacity_loads() if kind == "load" else [])]:
        key = (row.get("source"), row.get("ubatch"), row.get("n_ctx"), tuple(row.get("gpus") or []))
        if key in seen:
            continue
        seen.add(key)
        yield row


def cards_from(obs: dict | None, gpus: list[int]) -> list[dict]:
    """The named cards as the rig's latest scan describes them (UUID, link, root port), in the given order.
    Empty when the rig's report has no scan (an older rig.py)."""
    scan = {int(c["index"]): c for c in ((obs or {}).get("gpu_scan") or {}).get("cards") or []}
    return [{k: scan[g].get(k) for k in CARD_KEYS} for g in gpus if g in scan]
