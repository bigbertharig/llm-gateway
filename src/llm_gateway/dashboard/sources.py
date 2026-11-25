"""Format model evidence returned by Orchestration."""

from __future__ import annotations

from typing import Any

def benchmark_evidence_summary(summary) -> dict:
    """Format published model evidence returned by Orchestration."""
    models = summary.get("models") or []
    return {"source": "orchestration_model_catalog", "models": [
        {"model": m.get("model"), "status": m.get("status"), "scores": m.get("scores") or {},
         "runtime": m.get("runtime") or {}, "profiles": m.get("profiles") or []}
        for m in models], "key_tests": summary.get("key_tests") or [], "error": summary.get("error")}
