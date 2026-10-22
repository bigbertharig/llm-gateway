"""The dashboard (docs/DASHBOARD.md): one page at /dashboard, its data under /dashboard/api.

Rig: the manager's status, and the operator overrides, which go through the manager like `rigctl`
(kill, unload, unclaim, cancel, load). Models and Benchmarks: read-only summaries from the benchmarking
repo and the shared drive. Orchestration: llm_orchestration's own summary and plan controls.
"""

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
ORCHESTRATION_ACTIONS = ("start_plan", "resume_plan", "kill_plan", "kill_all_active", "batch_outputs",
                         "cleanup_stale", "requeue_worker")


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


def router(manager: Manager) -> APIRouter:
    r = APIRouter(prefix="/dashboard")

    @r.get("", include_in_schema=False)
    async def bare():
        return RedirectResponse("/dashboard/")

    @r.get("/", response_class=HTMLResponse)
    async def page():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- rig (the manager)

    @r.get("/api/rig")
    async def rig():
        return await manager.status()

    @r.post("/api/rig/kill")
    async def kill(request: Request):
        b = await _body(request)
        return await manager.kill(_gpus(b.get("gpu_ids")), str(b.get("reason") or "dashboard"))

    @r.post("/api/rig/unload")
    async def unload(request: Request):
        b = await _body(request)
        if not b.get("profile"):
            raise ManagerError(400, "give a profile")
        return await manager.unload_profile(str(b["profile"]))

    @r.post("/api/rig/unclaim")
    async def unclaim(request: Request):
        b = await _body(request)
        if not b.get("name"):
            raise ManagerError(400, "give a claim name")
        return {"released": await manager.remove_claim(str(b["name"]))}

    @r.post("/api/rig/run/{action}")
    async def run_action(action: str, request: Request):
        """Recover (resume from its checkpoint), dismiss or stop a run from the rig's report."""
        b = await _body(request)
        if not b.get("run"):
            raise ManagerError(400, "give a run id")
        run = str(b["run"])
        if action == "recover":
            return await manager.recover_run(run)
        if action == "dismiss":
            return await manager.dismiss_run(run, "dashboard")
        if action == "stop":
            return await manager.stop_run(run, "dashboard")
        raise ManagerError(404, f"no run action {action}")

    @r.post("/api/rig/cancel")
    async def cancel(request: Request):
        b = await _body(request)
        if not b.get("item"):
            raise ManagerError(400, "give a plan item id")
        return {"item": await manager.cancel_item(str(b["item"]))}

    @r.post("/api/rig/load")
    async def load(request: Request):
        b = await _body(request)
        if not b.get("model"):
            raise ManagerError(400, "give a model")
        gpu_ids = _gpus(b["gpu_ids"]) if b.get("gpu_ids") else None
        return await manager.load_model(
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
            claims = {c["name"] for c in (await manager.status()).get("claims", [])}
        except Exception:
            claims = None  # rig unreachable: fall back to log age
        try:
            return await asyncio.to_thread(sources.benchmarks_summary, None, claims)
        except (OSError, ValueError) as exc:
            return _unavailable(exc)

    # ---------------------------------------------------------------- orchestration (its own package)

    @r.get("/api/orchestration")
    async def orchestration():
        try:
            return await asyncio.to_thread(sources.orchestration_summary)
        except Exception as exc:  # the other repo's code: report, never take the gateway down
            return _unavailable(exc)

    @r.get("/api/orchestration/options")
    async def orchestration_options():
        try:
            return await asyncio.to_thread(lambda: sources.orchestration_controls().options())
        except Exception as exc:
            return _unavailable(exc)

    @r.post("/api/orchestration/{action}")
    async def orchestration_action(action: str, request: Request):
        if action not in ORCHESTRATION_ACTIONS:
            raise ManagerError(404, f"unknown orchestration action {action}; known: {', '.join(ORCHESTRATION_ACTIONS)}")
        b = await _body(request)

        def run():
            c = sources.orchestration_controls()
            if action == "start_plan":
                return c.start_plan(str(b.get("plan_name") or ""), str(b.get("config_json") or ""),
                                    str(b.get("plan_scope") or "shoulders"), str(b.get("starter_file") or ""),
                                    str(b.get("repo_path") or ""), str(b.get("repo_url") or ""),
                                    str(b.get("claimed_behavior") or ""), str(b.get("inline_query_text") or ""))
            if action in ("resume_plan", "kill_plan", "batch_outputs"):
                return getattr(c, action)(str(b.get("batch_id") or ""))
            if action == "requeue_worker":
                return c.requeue_worker(str(b.get("worker") or ""))
            return getattr(c, action)()

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:
            return _unavailable(exc)

    return r
