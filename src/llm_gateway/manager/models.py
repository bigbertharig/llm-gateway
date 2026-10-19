"""Registry models for "load model X on N cards" (MANAGER.md, "Model loads").

The benchmarking repo's model registry is the one source of truth for how a model loads; this module
reads it through the registry's own resolver (`scripts/active/model_library.py`) and turns one model
plus a use into a load spec the rig can run without a published remote profile.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any

# Pi: the drive is local; rig: its own checkout.
BENCHMARKING_ROOTS = (
    Path("/media/bryan/shared/plans/shoulders/benchmarking"),
    Path.home() / "llm_orchestration/shared/plans/shoulders/benchmarking",
)
# Catalog GGUF paths are written as the rig sees the drive; on the Pi it is mounted elsewhere.
CATALOG_SHARED = "/mnt/shared"
LOCAL_SHARED_CANDIDATES = ("/media/bryan/shared", "/mnt/shared")
# Registry profile per use when the request names none (falls back to `default`).
PROFILE_BY_USE = {"benchmark": "default", "chat": "interactive", "worker": "orchestration"}
# Lease controller type per use (the rig's lease record groups leases by it).
CONTROLLER_BY_USE = {"benchmark": "benchmarks", "chat": "interactive", "worker": "plans"}

_library: tuple[Path, ModuleType] | None = None


def benchmarking_root() -> Path:
    env = os.getenv("BENCHMARKING_ROOT")
    for root in [Path(env)] if env else BENCHMARKING_ROOTS:
        if (root / "model_registry.json").is_file():
            return root
    raise FileNotFoundError("model registry not found; set BENCHMARKING_ROOT")


def library() -> tuple[Path, ModuleType]:
    global _library
    root = benchmarking_root()
    if _library is None or _library[0] != root:
        spec = importlib.util.spec_from_file_location("rig_model_library", root / "scripts/active/model_library.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _library = (root, mod)
    return _library


def local_path(catalog_path: str) -> str:
    """Where this machine can read a catalog GGUF path (/mnt/shared/... on the rig)."""
    if not catalog_path.startswith(CATALOG_SHARED + "/"):
        return catalog_path
    for root in [os.getenv("SHARED_ROOT")] if os.getenv("SHARED_ROOT") else LOCAL_SHARED_CANDIDATES:
        if root and os.path.isdir(root):
            return root + catalog_path[len(CATALOG_SHARED):]
    return catalog_path


def profile_id(model: str, gpus: list[int]) -> str:
    """Rig profile id for a model load: the model plus its cards, e.g. ornith-1.5-9b-g1-3."""
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", model).strip("-").lower()
    return f"{slug}-g{'-'.join(str(g) for g in sorted(gpus))}"


def resolve(model: str, use: str, profile: str | None = None) -> dict[str, Any]:
    """Merged registry settings for the model and use: {model, profile, status, gguf_path, runtime,
    extra_args, image}. KeyError for an unknown model or profile."""
    root, lib = library()
    registry = lib.load_registry(root / "model_registry.json")
    _, entry = lib.find_model(registry, model)
    name = profile or PROFILE_BY_USE.get(use, "default")
    if name not in entry["profiles"]:
        if profile:
            raise KeyError(f"model {model} has no profile {profile}; profiles: {sorted(entry['profiles'])}")
        name = "default"
    r = lib.resolve(model, name, registry)
    return {**r, "image": lib.runtime_image(registry)}
