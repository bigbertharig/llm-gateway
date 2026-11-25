"""Desktop Cloud settings API."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request

from ..manager.errors import ManagerError
from .claude_code import ClaudeCodeBridge, ClaudeCodeError
from .cloud_settings import CloudSettings, CloudSettingsError
from .codex_bridge import CodexBridge, CodexBridgeError


def router(settings: CloudSettings, codex: CodexBridge | None = None,
           claude_code: ClaudeCodeBridge | None = None,
           codex_api: CodexBridge | None = None) -> APIRouter:
    r = APIRouter(prefix="/dashboard/api/cloud")

    async def body(request: Request) -> dict:
        try:
            value = await request.json()
        except ValueError as exc:
            raise ManagerError(400, "body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ManagerError(400, "body must be an object")
        return value

    @r.get("/settings")
    async def get_settings():
        try:
            result = settings.status()
            if codex is not None:
                try:
                    result["codex_subscription"] = await codex.account()
                except CodexBridgeError as exc:
                    result["codex_subscription"] = {"connected": False, "error": str(exc)}
            if codex_api is not None:
                try:
                    result["codex_api"] = await codex_api.account()
                except CodexBridgeError as exc:
                    result["codex_api"] = {"connected": False, "error": str(exc)}
            if claude_code is not None:
                result["claude_code"] = await claude_code.account()
            return result
        except CloudSettingsError as exc:
            raise ManagerError(500, str(exc)) from exc

    @r.post("/openrouter")
    async def save_openrouter(request: Request):
        value = await body(request)
        models = value.get("models")
        if not isinstance(models, list) or not all(isinstance(item, str) for item in models):
            raise ManagerError(400, "models must be a list of OpenRouter model IDs")
        try:
            if value.get("clear_api_key"):
                settings.clear_secret("openrouter")
            elif isinstance(value.get("api_key"), str) and value["api_key"].strip():
                settings.set_secret("openrouter", value["api_key"])
            settings.set_openrouter_models(models)
            return settings.status()["openrouter"]
        except CloudSettingsError as exc:
            raise ManagerError(400, str(exc)) from exc

    @r.post("/claude")
    async def save_claude(request: Request):
        value = await body(request)
        model = value.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ManagerError(400, "Claude model ID is required")
        try:
            if value.get("clear_api_key"):
                settings.clear_secret("claude")
            elif isinstance(value.get("api_key"), str) and value["api_key"].strip():
                settings.set_secret("claude", value["api_key"])
            settings.set_claude_model(model)
            return settings.status()["claude"]
        except CloudSettingsError as exc:
            raise ManagerError(400, str(exc)) from exc

    @r.post("/codex/api-key")
    async def codex_api_key(request: Request):
        if codex_api is None:
            raise ManagerError(503, "Codex authentication is unavailable")
        value = await body(request)
        api_key = value.get("api_key")
        if not isinstance(api_key, str):
            raise ManagerError(400, "OpenAI API key is required")
        try:
            return await codex_api.login_api_key(api_key)
        except CodexBridgeError as exc:
            raise ManagerError(503, str(exc)) from exc

    @r.post("/codex/login")
    async def codex_login():
        if codex is None:
            raise ManagerError(503, "Codex authentication is unavailable")
        try:
            return await codex.start_login()
        except CodexBridgeError as exc:
            raise ManagerError(503, str(exc)) from exc

    @r.post("/codex/logout")
    async def codex_logout():
        if codex is None:
            raise ManagerError(503, "Codex authentication is unavailable")
        try:
            await codex.logout()
        except CodexBridgeError as exc:
            raise ManagerError(503, str(exc)) from exc
        return {"connected": False}

    @r.post("/codex/api-logout")
    async def codex_api_logout():
        if codex_api is None:
            raise ManagerError(503, "Codex API authentication is unavailable")
        try:
            await codex_api.logout()
        except CodexBridgeError as exc:
            raise ManagerError(503, str(exc)) from exc
        return {"connected": False}

    @r.post("/claude-code/logout")
    async def claude_code_logout():
        if claude_code is None:
            raise ManagerError(503, "Claude Code authentication is unavailable")
        try:
            await claude_code.logout()
        except ClaudeCodeError as exc:
            raise ManagerError(503, str(exc)) from exc
        return {"connected": False}

    @r.post("/claude-code/login")
    async def claude_code_login():
        if claude_code is None:
            raise ManagerError(503, "Claude Code authentication is unavailable")
        try:
            return await claude_code.start_login()
        except ClaudeCodeError as exc:
            raise ManagerError(503, str(exc)) from exc

    @r.post("/claude-code/login/complete")
    async def claude_code_login_complete(request: Request):
        if claude_code is None:
            raise ManagerError(503, "Claude Code authentication is unavailable")
        value = await body(request)
        code = value.get("code")
        if not isinstance(code, str):
            raise ManagerError(400, "Claude authorization code is required")
        try:
            return await claude_code.complete_login(code)
        except ClaudeCodeError as exc:
            raise ManagerError(503, str(exc)) from exc

    @r.get("/openrouter/models")
    async def discover_openrouter():
        try:
            key, _models = settings.openrouter_configuration()
        except CloudSettingsError as exc:
            raise ManagerError(400, str(exc)) from exc
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
                response = await client.get(settings.openrouter_base_url + "/models",
                                            headers={"Authorization": "Bearer " + key})
            if response.status_code >= 400:
                raise ManagerError(502, f"OpenRouter returned HTTP {response.status_code}")
            rows = response.json().get("data") or []
        except httpx.HTTPError as exc:
            raise ManagerError(502, "OpenRouter model discovery failed") from exc
        result = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
            def per_million(value):
                try:
                    amount = float(value) * 1_000_000
                except (TypeError, ValueError):
                    return None
                return round(amount, 9) if math.isfinite(amount) and amount >= 0 else None
            release_year = None
            try:
                created = float(row.get("created"))
                if created > 100_000_000_000:
                    created /= 1000
                year = datetime.fromtimestamp(created, timezone.utc).year
                if 2000 <= year <= datetime.now(timezone.utc).year + 1:
                    release_year = year
            except (TypeError, ValueError, OverflowError, OSError):
                pass
            result.append({"id": row["id"], "name": row.get("name") or row["id"],
                           "context_length": row.get("context_length"),
                           "prompt_price_per_million": per_million(pricing.get("prompt")),
                           "completion_price_per_million": per_million(pricing.get("completion")),
                           "release_year": release_year,
                           "description": row.get("description") or ""})
        result.sort(key=lambda item: item["id"].lower())
        return {"models": result}

    return r
