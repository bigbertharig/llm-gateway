"""HTTP API of the rig manager, mounted at /v1/manager (docs/MANAGER.md, "The client process")."""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Request

from . import fit
from .core import Manager
from .errors import ManagerError


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ManagerError(400, f"invalid JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise ManagerError(400, "body must be a JSON object")
    return body


def _require(body: dict, *keys: str) -> None:
    missing = [k for k in keys if not body.get(k)]
    if missing:
        raise ManagerError(400, f"missing field(s): {', '.join(missing)}")


def router(manager: Manager, profiles: Callable[[], dict[str, dict]]) -> APIRouter:
    r = APIRouter(prefix="/v1/manager")

    @r.get("/status")
    async def status():
        return await manager.status()

    @r.get("/leases")
    async def leases():
        return {"leases": await manager.leases(), "claims": await manager.claims()}

    @r.post("/leases", status_code=201)
    async def acquire(request: Request):
        b = await _body(request)
        _require(b, "controller", "owner", "run_id")
        lease = await manager.acquire(
            controller=b["controller"], owner=b["owner"], run_id=b["run_id"],
            gpus_needed=b.get("gpus_needed"), gpu_ids=b.get("gpu_ids"), claim=b.get("claim"),
            ttl_s=int(b.get("ttl_s", 120)), model=b.get("model", ""), purpose=b.get("purpose", ""),
            lane=b.get("lane"))
        return {"lease": lease, "port": lease["metadata"]["port"]}

    @r.post("/leases/{lease_id}/renew")
    async def renew(lease_id: str, request: Request):
        b = await _body(request)
        _require(b, "owner")
        return {"lease": await manager.renew(lease_id, b["owner"], int(b.get("ttl_s", 120)))}

    @r.post("/leases/{lease_id}/release")
    async def release(lease_id: str, request: Request):
        b = await _body(request)
        if not b.get("force"):
            _require(b, "owner")
        await manager.release(lease_id, b.get("owner", ""), force=bool(b.get("force")))
        return {"released": lease_id}

    @r.get("/claims")
    async def claims():
        return {"claims": await manager.claims()}

    @r.post("/claims", status_code=201)
    async def add_claim(request: Request):
        """Take cards out of the pool, or reserve them while they are still leased (a split waiting for
        its cards). ttl_s: the claim lapses unless its owner renews it."""
        b = await _body(request)
        _require(b, "name", "gpu_ids")
        return {"claim": await manager.add_claim(
            b["name"], b["gpu_ids"], b.get("note", ""), b.get("by", ""), owner=b.get("owner", ""),
            ttl_s=int(b["ttl_s"]) if b.get("ttl_s") else None, purpose=b.get("purpose", ""),
            model=b.get("model", ""))}

    @r.post("/claims/{name}/renew")
    async def renew_claim(name: str, request: Request):
        b = await _body(request)
        return {"claim": await manager.renew_claim(name, b.get("owner", ""),
                                                   int(b["ttl_s"]) if b.get("ttl_s") else None)}

    @r.post("/claims/{name}/release")
    async def remove_claim(name: str):
        return {"released": await manager.remove_claim(name)}

    @r.post("/kill")
    async def kill(request: Request):
        """Operator override: stop every runtime on these cards, release their leases, drop their claims."""
        b = await _body(request)
        _require(b, "gpu_ids")
        return await manager.kill([int(g) for g in b["gpu_ids"]], b.get("reason", ""))

    @r.post("/profiles/{profile_id}/load")
    async def load(profile_id: str, request: Request):
        published = profiles()
        if profile_id not in published:
            raise ManagerError(404, f"unknown profile {profile_id}; published: {sorted(published)}")
        b = await _body(request)
        port = await manager.rig.ensure_ready(
            profile_id, start=lambda: manager.load_profile(profile_id, published[profile_id], b.get("claim")))
        return {"profile": profile_id, "port": port, "state": "ready"}

    @r.post("/profiles/{profile_id}/unload")
    async def unload(profile_id: str):
        return await manager.unload_profile(profile_id)

    def _model_request(b: dict) -> dict:
        _require(b, "model")
        ctx = b.get("ctx")
        return {"cards": int(b["cards"]) if b.get("cards") else None, "gpu_ids": b.get("gpu_ids"),
                "ctx": None if ctx in (None, "", "auto") else int(ctx),
                "ctx_min": int(b.get("ctx_min") or fit.CTX_MIN_DEFAULT), "use": b.get("use", "benchmark"),
                "profile": b.get("profile"), "claim": b.get("claim"),
                "margin_mb": int(b.get("margin_mb") or fit.MARGIN_MB_DEFAULT)}

    @r.post("/models/fit")
    async def fit_model(request: Request):
        """Would `model` fit on `cards` cards (or `gpu_ids`), and at what context? Nothing is loaded."""
        b = await _body(request)
        req = _model_request(b)
        return await manager.fit_model(b["model"], **req)

    @r.post("/models/load")
    async def load_model(request: Request):
        """Fit, lease, load on the rig, wait until it serves. Unload with /profiles/<profile>/unload.
        wait_s: reserve busy cards and wait for them (MANAGER.md, "Reservations"); force: kill what runs
        on the named cards first (operator override)."""
        b = await _body(request)
        req = _model_request(b)
        return await manager.load_model(b["model"], extra_args=[str(a) for a in b.get("extra_args") or []],
                                        wait_s=float(b.get("wait_s") or 0), force=bool(b.get("force")),
                                        owner=str(b.get("owner") or ""), **req)

    @r.get("/runs")
    async def runs():
        return {"runs": await manager.runs()}

    @r.post("/runs", status_code=201)
    async def start_run(request: Request):
        """Hand the rig a long run (campaign, lane) as a job in the shared report: {run, argv, owner?, cwd?,
        env?, resume_argv?, auto_resume?, needs? [{card_mb, count}], note?}."""
        b = await _body(request)
        _require(b, "run", "argv")
        return await manager.start_run(str(b["run"]), [str(a) for a in b["argv"]], owner=str(b.get("owner") or ""),
                                       cwd=b.get("cwd"), env=b.get("env") or {},
                                       resume_argv=[str(a) for a in b["resume_argv"]] if b.get("resume_argv") else None,
                                       auto_resume=bool(b.get("auto_resume")), needs=b.get("needs") or [],
                                       note=str(b.get("note") or ""))

    @r.post("/runs/{run}/recover")
    async def recover_run(run: str):
        return await manager.recover_run(run)

    @r.post("/runs/{run}/stop")
    async def stop_run(run: str, request: Request):
        b = await _body(request)
        return await manager.stop_run(run, str(b.get("reason") or ""))

    @r.post("/runs/{run}/dismiss")
    async def dismiss_run(run: str, request: Request):
        b = await _body(request)
        return await manager.dismiss_run(run, str(b.get("reason") or ""))

    @r.post("/owners/{who}/cancel")
    async def cancel_owner(who: str, request: Request):
        """Cancel everything one owner has on the rig: runs, queued items, loads, leases, claims, waits."""
        b = await _body(request)
        return await manager.cancel_owner(who, str(b.get("reason") or ""))

    @r.post("/reconcile")
    async def reconcile():
        return await manager.reconcile_once()

    @r.get("/controller")
    async def controller():
        await manager.observe(fresh=True)
        return manager.control()

    @r.post("/controller/claim")
    async def claim_control(request: Request):
        """Take control: when there is none or it is stale; {"force": true} is a handoff."""
        b = await _body(request)
        taken = await manager.take_control(force=bool(b.get("force")))
        await manager.observe(fresh=True)
        return {"taken": taken, **manager.control()}

    @r.post("/controller/release")
    async def release_control():
        await manager.release_control()
        return manager.control()

    @r.get("/plan")
    async def plan():
        return await manager.plan()

    @r.post("/plan/{item_id}/cancel")
    async def cancel_item(item_id: str):
        return {"item": await manager.cancel_item(item_id)}

    return r
