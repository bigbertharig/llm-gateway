"""HTTP API of the rig manager, mounted at /v1/manager (docs/MANAGER.md, "The client process")."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import APIRouter, Request

from . import fit, models, observations
from .core import Manager
from .errors import ManagerError
from ..rig import RigUnavailable


# Agent capability policy names reviewed benchmarking roles, never models or GPUs.
# Ready routes favor quality; cold loads try smaller workers first.
AGENT_ROLE_ORDER = {
    "coding": ("brain_code", "single_1060_code"),
    "research": ("brain", "single_1060", "text_extraction"),
}


def _agent_profile_order(agent_type: str, published: dict[str, dict], *, cold: bool = False) -> list[str]:
    catalog = models.catalog()
    entries = catalog.get("models", {})
    roles = catalog.get("roles", {})
    order = list(AGENT_ROLE_ORDER[agent_type])
    if cold:
        order.reverse()
    selected = []
    for role_name in order:
        model = (roles.get(role_name) or {}).get("model")
        entry = entries.get(model, {})
        if entry.get("status") != "active" or entry.get("in_testing"):
            continue
        for pid, profile in published.items():
            if profile.get("model") == model and pid not in selected:
                selected.append(pid)
    return selected


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


def router(manager: Manager, profiles: Callable[[], dict[str, dict]], model_prefix: str = "gpu-rig") -> APIRouter:
    r = APIRouter(prefix="/v1/manager")

    @r.get("/status")
    async def status():
        return await manager.status()

    @r.get("/model-offers")
    async def model_offers():
        """Reviewed capabilities for runtime clients; benchmark candidates stay private."""
        catalog = models.catalog()
        available = catalog.get("models", {})
        data = []
        for capability, role in sorted(catalog.get("roles", {}).items()):
            model = role.get("model")
            entry = available.get(model, {})
            if entry.get("status") != "active" or entry.get("in_testing"):
                continue
            data.append({"capability": capability, "model": model,
                         "profile": role.get("profile") or "default",
                         "good_for": role.get("good_for") or [],
                         "description": role.get("why") or ""})
        return {"object": "model_offers", "version": 1, "data": data}

    @r.get("/chat/availability")
    async def chat_availability(profile_id: str | None = None):
        if profile_id is not None and profile_id not in profiles():
            raise ManagerError(404, "unknown published profile")
        return await manager.chat_availability(profile_id)

    @r.post("/agent-routes/resolve")
    async def resolve_agent_route(request: Request):
        """Offer an already-ready, disjoint route for a child agent capability."""
        b = await _body(request)
        agent_type, parent_id = b.get("agent_type"), b.get("parent_route")
        if agent_type not in AGENT_ROLE_ORDER or not isinstance(parent_id, str):
            raise ManagerError(400, f"agent_type must be one of {sorted(AGENT_ROLE_ORDER)} and parent_route is required")
        prefix = model_prefix + "/"
        if not parent_id.startswith(prefix):
            raise ManagerError(400, "parent_route must be a gateway route")
        min_context = b.get("min_context", 4096)
        if type(min_context) is not int or min_context < 1:
            raise ManagerError(400, "min_context must be a positive integer")
        observed = await manager.status()
        if observed.get("from_copy"):
            raise ManagerError(503, "rig observation is stale")
        loaded = observed.get("profiles") or {}
        if isinstance(loaded, list):
            loaded = {row.get("id"): row for row in loaded if isinstance(row, dict)}
        parent = loaded.get(parent_id[len(prefix):])
        if not parent or parent.get("state") != "ready" or not parent.get("gpus"):
            raise ManagerError(409, "parent route is not ready with known GPU placement")
        parent_gpus = set(parent["gpus"])
        published = profiles()
        for pid in _agent_profile_order(agent_type, published):
            row, spec = loaded.get(pid), published.get(pid)
            if not row or not spec or row.get("state") != "ready" or row.get("model") != spec.get("model"):
                continue
            gpus, context = row.get("gpus"), row.get("ctx_size")
            if (not gpus or not parent_gpus.isdisjoint(gpus) or
                    type(context) is not int or context < min_context or not row.get("port")):
                continue
            return {"agent_type": agent_type, "route": prefix + pid, "model": row["model"],
                    "gpu_ids": sorted(gpus), "context_length": context, "state": "ready"}
        # No ready offer: let the manager fit, lease, and load a published profile.
        # Existing ready routes on the parent's cards are never moved by this request.
        async def prepare_cold():
            failures = []
            for pid in _agent_profile_order(agent_type, published, cold=True):
                spec = published.get(pid)
                if not spec or pid in loaded:
                    continue
                try:
                    await manager.rig.ensure_ready(pid, start=lambda pid=pid, spec=spec: manager.load_profile(pid, spec))
                except (ManagerError, RigUnavailable) as exc:
                    if exc.status not in (409, 422, 503):
                        raise
                    failures.append(f"{pid}: {exc}")
                    continue
                refreshed = await manager.status()
                if refreshed.get("from_copy"):
                    raise ManagerError(503, "rig observation is stale after child load")
                rows = refreshed.get("profiles") or {}
                if isinstance(rows, list):
                    rows = {row.get("id"): row for row in rows if isinstance(row, dict)}
                parent_now, row = rows.get(parent_id[len(prefix):]), rows.get(pid)
                if (parent_now and parent_now.get("state") == "ready" and row and row.get("state") == "ready"
                        and row.get("model") == spec.get("model") and row.get("gpus")
                        and set(parent_now.get("gpus") or []).isdisjoint(row["gpus"])
                        and type(row.get("ctx_size")) is int and row["ctx_size"] >= min_context):
                    return {"agent_type": agent_type, "route": prefix + pid, "model": row["model"],
                            "gpu_ids": sorted(row["gpus"]), "context_length": row["ctx_size"], "state": "ready"}
                failures.append(f"{pid}: loaded route did not satisfy child offer")
            raise ManagerError(503, f"no {agent_type} route on GPUs separate from {parent_id}", failures=failures)
        try:
            return await asyncio.wait_for(prepare_cold(), timeout=300)
        except TimeoutError as exc:
            raise ManagerError(504, f"{agent_type} route was not ready within 300 seconds") from exc

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
        gpu_ids = b.get("gpu_ids")
        if gpu_ids is not None and (not isinstance(gpu_ids, list) or not gpu_ids
                                    or any(type(g) is not int or g < 0 for g in gpu_ids)
                                    or len(set(gpu_ids)) != len(gpu_ids)):
            raise ManagerError(400, "gpu_ids must be a nonempty list of distinct nonnegative GPU indexes")
        port = await manager.rig.ensure_ready(
            profile_id, start=lambda: manager.load_profile(profile_id, published[profile_id], b.get("claim"), gpu_ids))
        if gpu_ids is not None:
            loaded = (await manager.observe(fresh=True)).get("profiles", {}).get(profile_id)
            if not loaded or sorted(loaded.get("gpus") or []) != sorted(gpu_ids):
                raise ManagerError(409, f"profile {profile_id} is ready on a different GPU set")
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
                "margin_mb": int(b["margin_mb"]) if b.get("margin_mb") is not None else fit.MARGIN_MB_DEFAULT,
                "batch_size": int(b["batch_size"]) if b.get("batch_size") else None,
                "context_mode": b.get("context_mode", "working"),
                "engine": b.get("engine", "auto")}

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

    @r.get("/observations")
    async def list_observations(kind: str = "load", model: str | None = None, limit: int = 200):
        """Every load the manager knows of (observations.py): its log in benchmarking plus, for `load`, the
        loads in the capacity records, newest last; `limit` 0 for all."""
        got = [r for r in observations.history(kind) if model is None or r.get("model") == model]
        return {"path": observations.path(), "count": len(got), "rows": got[-limit:] if limit else got}

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
