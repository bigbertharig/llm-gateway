"""Gateway-owned dashboard reads from the imported model catalog."""

from __future__ import annotations

from typing import Any

from ..manager import models as registry_models

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
