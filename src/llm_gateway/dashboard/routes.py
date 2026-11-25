"""Gateway dashboard routes: manager status/actions and imported evidence."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..manager.core import Manager
from ..manager.errors import ManagerError
from . import sources

STATIC = Path(__file__).parent / "static"


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError as exc:
        raise ManagerError(400, f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ManagerError(400, "body must be a JSON object")
    return body


def _gpus(value: Any) -> list[int]:
    if isinstance(value, str):
        value = [v for v in value.replace(" ", "").split(",") if v]
    try:
        gpus = sorted({int(g) for g in value or []})
    except (TypeError, ValueError) as exc:
        raise ManagerError(400, f"gpu ids must be numbers: {value!r}") from exc
    if not gpus:
        raise ManagerError(400, "give at least one GPU id")
    return gpus


def _unavailable(exc: Exception) -> JSONResponse:
    return JSONResponse({"error": {"message": f"{type(exc).__name__}: {exc}", "type": "dashboard_source"}},
                        status_code=503)


def require_manager(manager):
    if manager is None:
        raise ManagerError(503, 'Local runtime integration is unavailable')
    return manager


def router(manager: Manager) -> APIRouter:
    r = APIRouter(prefix="/dashboard")

    @r.get("", include_in_schema=False)
    async def bare():
        return RedirectResponse("/dashboard/")

    @r.get("/", response_class=HTMLResponse)
    async def page():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8").replace(
                                "<body>", '<body data-runtime-enabled="' + ("true" if manager is not None else "false") + '">'),
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- rig (the manager)

    @r.get("/api/rig")
    async def rig():
        status = await require_manager(manager).status()
        # Shared-weight runtimes are owned by orchestration and do not appear in
        # the manager's ordinary profile list. Include their read-only status.
        try:
            service = (await require_manager(manager).rig.status()).get("shared_kv_service")
        except Exception:
            service = None
        return {**status, "shared_model_service": service}

    @r.post("/api/rig/kill")
    async def kill(request: Request):
        b = await _body(request)
        return await require_manager(manager).kill(_gpus(b.get("gpu_ids")), str(b.get("reason") or "dashboard"))

    @r.post("/api/rig/unload")
    async def unload(request: Request):
        b = await _body(request)
        if not b.get("profile"):
            raise ManagerError(400, "give a profile")
        return await require_manager(manager).unload_profile(str(b["profile"]))

    @r.post("/api/rig/unclaim")
    async def unclaim(request: Request):
        b = await _body(request)
        if not b.get("name"):
            raise ManagerError(400, "give a claim name")
        return {"released": await require_manager(manager).remove_claim(str(b["name"]))}

    @r.post("/api/rig/run/{action}")
    async def run_action(action: str, request: Request):
        """Recover (resume from its checkpoint), dismiss or stop a run from the rig's report."""
        b = await _body(request)
        if not b.get("run"):
            raise ManagerError(400, "give a run id")
        run = str(b["run"])
        if action == "recover":
            return await require_manager(manager).recover_run(run)
        if action == "dismiss":
            return await require_manager(manager).dismiss_run(run, "dashboard")
        if action == "stop":
            return await require_manager(manager).stop_run(run, "dashboard")
        raise ManagerError(404, f"no run action {action}")

    @r.post("/api/rig/cancel")
    async def cancel(request: Request):
        b = await _body(request)
        if not b.get("item"):
            raise ManagerError(400, "give a plan item id")
        return {"item": await require_manager(manager).cancel_item(str(b["item"]))}

    @r.post("/api/rig/load")
    async def load(request: Request):
        b = await _body(request)
        if not b.get("model"):
            raise ManagerError(400, "give a model")
        gpu_ids = _gpus(b["gpu_ids"]) if b.get("gpu_ids") else None
        return await require_manager(manager).load_model(
            str(b["model"]), gpu_ids=gpu_ids, cards=int(b["cards"]) if b.get("cards") and not gpu_ids else None,
            use=str(b.get("use") or "chat"), extra_args=shlex.split(str(b.get("args") or "")),
            wait_s=float(b.get("wait_s") or 0), force=bool(b.get("force")))

    # ---------------------------------------------------------------- models and benchmarks

    @r.get("/api/models")
    async def models():
        try:
            return await asyncio.to_thread(sources.models_summary)
        except (OSError, ValueError) as exc:
            return _unavailable(exc)

    @r.get("/api/benchmarks")
    async def benchmarks():
        try:
            return await asyncio.to_thread(sources.benchmark_evidence_summary)
        except (OSError, ValueError) as exc:
            return _unavailable(exc)

    return r
