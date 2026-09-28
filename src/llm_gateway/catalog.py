from __future__ import annotations

import json
from pathlib import Path


class CatalogError(RuntimeError):
    pass


def load_openrouter_models(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CatalogError(f"model catalog not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"cannot read model catalog: {path}") from exc

    if payload.get("schema_version") != 1:
        raise CatalogError(f"unsupported model catalog schema: {path}")

    configured = payload.get("openrouter_models")
    if not isinstance(configured, list):
        raise CatalogError("openrouter_models must be a list")

    models: list[str] = []
    for item in configured:
        if isinstance(item, str):
            model_id = item.strip()
            enabled = True
        elif isinstance(item, dict):
            model_id = str(item.get("id", "")).strip()
            enabled = bool(item.get("enabled", True))
        else:
            raise CatalogError("each OpenRouter model must be a string or object")
        if not model_id or "/" not in model_id:
            raise CatalogError(f"invalid OpenRouter model id: {model_id!r}")
        if enabled and model_id not in models:
            models.append(model_id)
    return models

