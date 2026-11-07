#!/usr/bin/env python3
"""Import the benchmarking model library into the gateway's own files: the only place the gateway reads it.

The model registry (benchmarking repo, model_registry.json) is where benchmarking keeps what it has learned
about how each model loads: GGUF, context, batch, extra args, per named profile. This importer copies that
into files the gateway owns, so the gateway runs with no benchmarking checkout:

- model_catalog.json: every registry model and its resolved profiles, the roles, the runtime image, and the
  dashboard's model summary (scores, runtime). The manager plans and loads from it (manager/models.py).
- remote_profiles.json: the offer, the models the gateway publishes on /v1/models under which registry
  profile. The offer names no cards: the manager's fit places a published profile and sizes its context.

Re-run it after benchmarking learns something new.

Usage (on the Pi, where the benchmarking repo is on the shared drive):
  tools/generate_profiles.py            rewrite both files
  tools/generate_profiles.py --check    exit 1 if the offer is invalid or either file is stale
  BENCHMARKING_ROOT=<path> ...          a different benchmarking checkout
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPO / "remote_profiles.json"
CATALOG_PATH = REPO / "model_catalog.json"
BENCHMARKING_ROOTS = (
    Path("/media/bryan/shared/plans/shoulders/benchmarking"),          # Pi
    Path.home() / "llm_orchestration/shared/plans/shoulders/benchmarking",  # rig
)
# Registry statuses the gateway may offer. Candidates are allowed so they can be tried remotely.
OFFERABLE_STATUSES = {"active", "candidate"}
USES = ("chat", "worker", "benchmark")  # manager/memory.py USES

# The offer: what the gateway publishes, under which registry profile. Model settings come from that profile,
# never from this list, and so do card choice and context: the manager's fit places each published profile
# on the cards it fits best and plans the largest context that fits, at least the profile's ctx_size.
# use (chat | worker | benchmark, default chat) sizes the host prompt cache (manager/memory.py).
OFFER: list[dict[str, Any]] = [
    {"id": "chat-27b", "dashboard_chat": True, "model": "qwen3.6:27b", "profile": "interactive", "default": True,
     "description": "Strong general/coding model for interactive agents (Roo)."},
    {"id": "coder-7b", "model": "qwen2.5-coder:7b", "profile": "interactive", "default": True,
     "description": "Fast small coder for scoped/bulk tasks. No native tool calls."},
    {"id": "coder-30b", "model": "qwen3-coder:30b-a3b", "profile": "interactive", "default": False,
     "description": "Coding MoE (30B, 3B active). Roo candidate."},
    {"id": "devstral-24b", "model": "devstral-small:24b", "profile": "interactive", "default": False,
     "description": "Agentic coding model (built for tool-using agents). Roo candidate."},
    {"id": "gpt-oss-20b", "dashboard_chat": True, "model": "gpt-oss:20b", "profile": "interactive", "default": False,
     "description": "MoE (21B, 3.6B active), strong tool calling, fast. Roo candidate."},
    {"id": "nex-split", "model": "nex-n2.5:mini", "profile": "interactive", "default": False,
     "description": "Nex N2.5 mini (35B MoE, 3B active)."},
    {"id": "harness-7b", "model": "qwen2.5-coder:7b", "profile": "interactive", "default": False,
     "description": "Separate 7B parent route for harness size comparisons."},
    {"id": "harness-14b", "model": "qwen2.5-coder:14b", "profile": "orchestration", "default": False,
     "description": "14B parent route for harness size comparisons."},
    {"id": "harness-gemma4-e4b-64k", "dashboard_chat": True, "model": "gemma-4:e4b", "profile": "default", "default": False,
     "ctx_size": 65536, "description": "One GTX 1060 parent route for harness comparison."},
    {"id": "harness-gemma4-12b-64k", "dashboard_chat": True, "model": "gemma-4:12b", "profile": "default", "default": False,
     "ctx_size": 65536, "description": "Two GTX 1060 parent route for harness comparison."},
    {"id": "harness-ministral-8b", "model": "ministral-3:8b", "profile": "default", "default": False,
     "description": "Alternate small-model parent route for harness tool comparisons."},
    {"id": "harness-ornith-9b", "model": "ornith-1.5:9b", "profile": "default", "default": False,
     "description": "Alternate small-model parent route for harness tool comparisons."},
    {"id": "harness-phi-14b", "model": "phi-4:14b", "profile": "default", "default": False,
     "description": "Alternate 14B parent route for harness tool comparisons."},
    {"id": "harness-74b", "model": "nemotron-3.5-bolt:74b-a9b", "profile": "default", "default": False,
     "description": "74B parent route for harness size comparisons."},
]

GENERATED_NOTE = ("Generated by tools/generate_profiles.py from the model registry. Do not edit; "
                  "change OFFER in the script or the registry profile, then re-run it.")
CATALOG_NOTE = ("Imported by tools/generate_profiles.py from the benchmarking model library. Do not edit; "
                "the gateway reads models only from here. Re-run the importer to take in new lessons.")


def benchmarking_root() -> Path:
    env = os.getenv("BENCHMARKING_ROOT")
    candidates = [Path(env)] if env else list(BENCHMARKING_ROOTS)
    for root in candidates:
        if (root / "model_registry.json").is_file():
            return root
    raise SystemExit(f"model registry not found under {[str(c) for c in candidates]}; set BENCHMARKING_ROOT")


def load_library(root: Path) -> ModuleType:
    path = root / "scripts" / "active" / "model_library.py"
    spec = importlib.util.spec_from_file_location("rig_model_library", path)
    library = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(library)
    return library


def build(offer: list[dict[str, Any]], library: Any, registry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(remote_profiles document, problems). Profiles with problems are left out."""
    problems: list[str] = []
    profiles = []
    seen = set()
    for item in offer:
        pid = item["id"]
        if pid in seen:
            problems.append(f"{pid}: offered twice")
            continue
        seen.add(pid)
        before = len(problems)
        try:
            r = library.resolve(item["model"], item.get("profile", "default"), registry)
        except KeyError as exc:
            problems.append(f"{pid}: {exc.args[0]}")
            continue
        runtime = r.get("runtime", {})
        if r["status"] not in OFFERABLE_STATUSES:
            problems.append(f"{pid}: {r['model']} is {r['status']}; offer only {sorted(OFFERABLE_STATUSES)} models")
        if "ctx_size" not in runtime:
            problems.append(f"{pid}: registry profile {r['profile']} lacks runtime ctx_size")
        if "gpus_needed" in item or "tensor_split" in item:
            problems.append(f"{pid}: the offer names no cards; the manager's fit places it")
        if item.get("use", "chat") not in USES:
            problems.append(f"{pid}: use must be one of {USES}")
        if len(problems) > before:
            continue
        profiles.append({
            "id": pid,
            "model": r["model"],
            "registry_profile": r["profile"],
            "status": r["status"],
            "description": item.get("description", ""),
            "default": bool(item.get("default", False)),
            "dashboard_chat": bool(item.get("dashboard_chat", False)),
            "use": item.get("use", "chat"),
            "ctx_size": item.get("ctx_size", runtime["ctx_size"]),  # minimum context for this route
        })
    doc = {"_comment": GENERATED_NOTE, "registry_updated": registry.get("updated"), "remote_profiles": profiles}
    return doc, problems


def build_catalog(library: Any, registry: dict[str, Any], roles: dict[str, Any] | None,
                  summary: dict[str, Any] | None) -> dict[str, Any]:
    """The gateway's copy of the model library: every model with its resolved profiles, the roles, the image,
    and the dashboard's model summary."""
    models = {}
    for mid, entry in registry["models"].items():
        if not isinstance(entry, dict):
            continue
        profiles, gguf = {}, None
        for name in entry.get("profiles") or {}:
            try:
                r = library.resolve(mid, name, registry)
            except KeyError:
                continue
            gguf = gguf or r.get("gguf_path")
            profiles[name] = {k: v for k, v in r.items() if k not in ("model", "profile", "status", "gguf_path")}
        model_size = entry.get("model_size_bytes")
        ngram_size = entry.get("ngram_size_bytes", 0)
        if model_size is not None:
            if type(model_size) is not int or model_size <= 0 or type(ngram_size) is not int or ngram_size < 0:
                raise ValueError(f"{mid}: invalid model_size_bytes or ngram_size_bytes")
        models[mid] = {"status": entry.get("status"), "aliases": entry.get("aliases", []), "gguf_path": gguf,
                       "model_size_bytes": model_size, "ngram_size_bytes": ngram_size,
                       "total_size_bytes": model_size + ngram_size if model_size is not None else None,
                       "strata": entry.get("strata"), "profiles": profiles}
    role_rows = {name: {k: spec.get(k) for k in ("model", "profile", "lane", "why")}
                 for name, spec in ((roles or {}).get("roles") or {}).items() if isinstance(spec, dict)}
    return {"_comment": CATALOG_NOTE, "registry_updated": registry.get("updated"),
            "roles_updated": (roles or {}).get("updated"), "image": library.runtime_image(registry),
            "models": models, "roles": role_rows, "summary": summary}


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=True) + "\n"


def _summary(root: Path) -> dict[str, Any] | None:
    """The dashboard's model summary, built here from the benchmarking results (the dashboard reads the copy)."""
    sys.path.insert(0, str(REPO / "src"))
    from llm_gateway.dashboard.sources import build_models_summary  # noqa: E402
    return build_models_summary(root)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="validate and compare, do not write")
    args = ap.parse_args()

    root = benchmarking_root()
    library = load_library(root)
    registry = library.load_registry(root / "model_registry.json")
    doc, problems = build(OFFER, library, registry)
    roles = json.loads((root / "model_roles.json").read_text(encoding="utf-8")) \
        if (root / "model_roles.json").is_file() else None
    catalog = build_catalog(library, registry, roles, _summary(root))
    for p in problems:
        print(f"problem: {p}")
    outputs = {OUTPUT_PATH: render(doc), CATALOG_PATH: render(catalog)}
    if args.check:
        for path, text in outputs.items():
            current = path.read_text(encoding="utf-8") if path.is_file() else ""
            # the summary carries its build time; compare without it
            strip = (lambda t: re.sub(r'"generated_at": "[^"]*"', "", t)) if path == CATALOG_PATH else (lambda t: t)
            if strip(current) != strip(text):
                problems.append(f"{path.name} stale")
                print(f"problem: {path.name} differs from the model library; run tools/generate_profiles.py")
        print("ok" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if problems:
        print(f"not written: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT_PATH.name}: {', '.join(p['id'] for p in doc['remote_profiles'])}")
    print(f"wrote {CATALOG_PATH.name}: {len(catalog['models'])} models, {len(catalog['roles'])} roles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
