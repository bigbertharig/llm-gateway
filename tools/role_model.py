"""Resolve a benchmarking role to its model, so tools never hard-code a model name.

    python3 tools/role_model.py [ROLE]          print the model id (default role: single_1060)
    python3 tools/role_model.py ROLE --json     model, profile, gguf_path, gguf_relative

Roles live in the benchmarking repo's model_roles.json (resolved by its scripts/active/model_library.py).
Change a role there and every tool that uses this follows. Standard library only.
"""
import json
import subprocess
import sys
from pathlib import Path

LIBRARY_CANDIDATES = [
    Path.home() / "llm_orchestration/shared/plans/shoulders/benchmarking/scripts/active/model_library.py",
    Path("/media/bryan/shared/plans/shoulders/benchmarking/scripts/active/model_library.py"),
    Path("/mnt/shared/plans/shoulders/benchmarking/scripts/active/model_library.py"),
]
DEFAULT_ROLE = "single_1060"


def _library(kind: str, name: str) -> dict:
    library = next((p for p in LIBRARY_CANDIDATES if p.exists()), None)
    if library is None:
        raise RuntimeError("model_library.py not found; looked in: " + ", ".join(map(str, LIBRARY_CANDIDATES)))
    p = subprocess.run([sys.executable, str(library), kind, name], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"{kind} {name!r}: {(p.stderr or p.stdout).strip()[-300:]}")
    return _summary(json.loads(p.stdout))


def resolve(role: str = DEFAULT_ROLE) -> dict:
    """{"model", "profile", "gguf_path", "gguf_relative"} for a role (gguf_relative is below models/)."""
    return _library("role", role)


def resolve_model(model: str) -> dict:
    """The same fields for a model id from the registry."""
    return _library("model", model)


def _summary(spec: dict) -> dict:
    gguf = spec.get("gguf_path") or ""
    return {"model": spec["model"], "profile": spec.get("profile", "default"), "gguf_path": gguf,
            "gguf_relative": gguf.split("/models/", 1)[1] if "/models/" in gguf else Path(gguf).name}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    info = resolve(args[0] if args else DEFAULT_ROLE)
    print(json.dumps(info) if "--json" in sys.argv else info["model"])
