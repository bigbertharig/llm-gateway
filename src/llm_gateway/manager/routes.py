"""HTTP API of the rig manager, mounted at /v1/manager (docs/MANAGER.md, "The client process")."""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, Request

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
        b = await _body(request)
        _require(b, "name", "gpu_ids")
        return {"claim": await manager.add_claim(b["name"], b["gpu_ids"], b.get("note", ""), b.get("by", ""))}

    @r.post("/claims/{name}/release")
    async def remove_claim(name: str):
        return {"released": await manager.remove_claim(name)}

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

    @r.post("/reconcile")
    async def reconcile():
        return await manager.reconcile_once()

    return r
