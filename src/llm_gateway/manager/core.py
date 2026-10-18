"""The rig manager: controller management (docs/MANAGER.md).

It decides who holds which GPUs and sends the rig one command per action. It keeps no allocation
state: the lease and claim record lives on the rig (store.RigRecord), and hardware rules (one load
at a time, health, power and thermal limits) are enforced there. The rig keeps running whatever it
was given if the controller goes away; this side observes it (`rig observe`) and reconciles.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..rig import Rig, RigUnavailable
from . import placement
from .errors import ManagerError
from .status import gpu_rows, is_expired
from .store import RigRecord

log = logging.getLogger("llm_gateway.manager")

OWNER = "llm-gateway"  # owner of the leases this process takes for interactive profiles
INTERACTIVE_TTL_S = 300
RECONCILE_EVERY_S = 30
OBSERVE_CACHE_S = 3.0
LOAD_POLL_S = 3.0
# A lease of ours with no rig profile or job behind it is released after this long (covers the
# moment between taking the lease and the rig recording the load).
ORPHAN_GRACE_S = 60
JOB_PREFIX = "rig-load-"  # rig hw_jobs unit prefix; the rest is the profile id


class Manager:
    def __init__(self, rig: Rig, start_timeout_s: float = 900):
        self.rig = rig
        self.record = RigRecord(rig)
        self.start_timeout_s = start_timeout_s
        self._obs: dict | None = None
        self._obs_at = 0.0
        self._obs_lock = asyncio.Lock()
        # This controller makes one placement decision at a time; the rig refuses overlaps across controllers.
        self._place_lock = asyncio.Lock()

    # ------------------------------------------------------------------ observation

    async def observe(self, fresh: bool = False) -> dict:
        async with self._obs_lock:
            if fresh or self._obs is None or time.monotonic() - self._obs_at > OBSERVE_CACHE_S:
                self._obs = await self.rig.observe()
                self._obs_at = time.monotonic()
            return self._obs

    async def leases(self) -> list[dict]:
        return (await self.observe(fresh=True)).get("leases", [])

    async def claims(self) -> list[dict]:
        return (await self.observe(fresh=True)).get("claims", [])

    async def status(self) -> dict:
        obs = await self.observe()
        leases, claims = obs.get("leases", []), obs.get("claims", [])
        rows = gpu_rows(obs, leases, claims)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        return {"observed_at": obs.get("observed_at"), "mode": obs.get("mode"), "summary": counts,
                "guard": obs.get("guard"), "gpus": rows, "profiles": obs.get("profiles", {}), "jobs": obs.get("jobs", []),
                "last_error": obs.get("last_error"), "state_error": obs.get("state_error"),
                "leases": leases, "claims": claims}

    # ------------------------------------------------------------------ leases

    async def acquire(self, *, controller: str, owner: str, run_id: str, gpus_needed: list[dict] | None = None,
                      gpu_ids: list[int] | None = None, claim: str | None = None, ttl_s: int = 120,
                      model: str = "", purpose: str = "", lane: str | None = None) -> dict:
        if bool(gpus_needed) == bool(gpu_ids):
            raise ManagerError(400, "give exactly one of gpus_needed or gpu_ids")
        async with self._place_lock:
            obs = await self.observe(fresh=True)
            leases, claims = obs.get("leases", []), obs.get("claims", [])
            try:
                free, why = placement.availability(obs, leases, claims, claim)
            except KeyError as exc:
                raise ManagerError(404, str(exc.args[0])) from exc
            if gpu_ids:
                gpus = sorted({int(g) for g in gpu_ids})
                blocked = {g: why.get(g, "unknown GPU") for g in gpus if g not in {f["index"] for f in free}}
                if blocked:
                    raise ManagerError(409, "GPU(s) not available: " +
                                       "; ".join(f"GPU {g} {r}" for g, r in blocked.items()), unavailable=blocked)
            else:
                gpus = placement.place(gpus_needed, free)
                if gpus is None:
                    raise ManagerError(503, f"no capacity for {gpus_needed}", free=[f["index"] for f in free],
                                       unavailable=why)
            port = placement.port_for(gpus)
            conflict = placement.port_conflict(port, obs, leases)
            if conflict:
                raise ManagerError(409, conflict)
            lease = await self.record.acquire({
                "controller": controller, "owner": owner, "run_id": run_id, "gpu_ids": gpus, "lane": lane,
                "ttl_s": int(ttl_s), "ports": placement.lease_ports(gpus),
                "metadata": {"port": port, "model": model, "purpose": purpose, "claim": claim}})
            self._obs = None  # the record changed
        log.info("lease %s %s/%s GPU %s :%s %s", lease["lease_id"], controller, owner, gpus, port, model)
        return lease

    async def renew(self, lease_id: str, owner: str, ttl_s: int = 120) -> dict:
        return await self.record.renew(lease_id, owner, ttl_s)

    async def release(self, lease_id: str, owner: str = "", force: bool = False) -> None:
        """Release a lease. Unless forced, its runtime must be gone (nothing on its GPUs)."""
        obs = await self.observe(fresh=True)
        lease = next((l for l in obs.get("leases", []) if l.get("lease_id") == lease_id), None)
        if lease is None:
            raise ManagerError(404, f"lease not found: {lease_id}")
        if not force:
            running = [c["name"] for c in obs.get("containers", []) if set(c.get("gpus", [])) & set(lease["gpu_ids"])]
            if running:
                raise ManagerError(409, f"lease {lease_id} still has runtimes on its GPUs: {running}; stop them first")
        await self.record.release(lease_id, owner, force)
        self._obs = None
        log.info("released %s%s", lease_id, " (forced)" if force else "")

    # ------------------------------------------------------------------ claims

    async def add_claim(self, name: str, gpu_ids: list[int], note: str = "", by: str = "") -> dict:
        claim = await self.record.add_claim(name, gpu_ids, note, by)
        self._obs = None
        return claim

    async def remove_claim(self, name: str) -> dict:
        claim = await self.record.remove_claim(name)
        self._obs = None
        return claim

    # ------------------------------------------------------------------ interactive profiles

    async def load_profile(self, profile_id: str, profile: dict, claim: str | None = None) -> None:
        """Lease cards for a profile, hand the load to the rig as one job, and wait for it to serve.

        The rig queues and runs the job (one load at a time is its rule, not ours). Past the start
        timeout the caller gets a 504 and the job keeps going; the lease stays with it.
        """
        lease = await self.acquire(controller="interactive", owner=OWNER, run_id=f"profile-{profile_id}",
                                   gpus_needed=profile["gpus_needed"], claim=claim, ttl_s=INTERACTIVE_TTL_S,
                                   model=profile.get("model", ""), purpose=f"gateway profile {profile_id}")
        lease_id = lease["lease_id"]
        try:
            job = await self.rig.start_detached(profile_id, lease["gpu_ids"], lease["metadata"]["port"], lease_id)
        except RigUnavailable:
            await self.release(lease_id, OWNER, force=True)
            raise
        log.info("load %s handed to the rig as %s", profile_id, job.get("job"))
        deadline = time.monotonic() + self.start_timeout_s
        seen = bool(job.get("already_running"))
        while True:
            obs = await self.observe(fresh=True)
            prof = obs.get("profiles", {}).get(profile_id)
            j = {x["job"]: x for x in obs.get("jobs", [])}.get(job.get("job"))
            if prof and prof.get("state") == "ready":
                return
            seen = seen or bool(prof) or bool(j)
            if j and j.get("active_state") == "failed":
                await self.release(lease_id, OWNER, force=True)
                raise RigUnavailable(502, f"rig failed to load {profile_id}: {j.get('log_tail', '')[-1500:]}")
            if seen and not prof and not j:
                await self.release(lease_id, OWNER, force=True)
                raise RigUnavailable(502, f"rig dropped {profile_id}: {obs.get('last_error') or 'no detail'}")
            if time.monotonic() > deadline:
                raise RigUnavailable(504, f"{profile_id} not ready within {self.start_timeout_s:.0f}s; "
                                          "the rig is still loading it, retry shortly")
            await asyncio.sleep(LOAD_POLL_S)

    async def unload_profile(self, profile_id: str) -> dict:
        obs = await self.observe(fresh=True)
        prof = obs.get("profiles", {}).get(profile_id)
        if not prof:
            raise ManagerError(404, f"{profile_id} is not loaded")
        await self.rig.stop_profile(profile_id)
        released = None
        if prof.get("lease") and any(l.get("lease_id") == prof["lease"] for l in obs.get("leases", [])):
            await self.release(prof["lease"], OWNER, force=True)
            released = prof["lease"]
        return {"unloaded": profile_id, "released": released}

    # ------------------------------------------------------------------ reconcile

    async def reconcile_once(self) -> dict:
        """Renew our leases that still have a load or runtime behind them; release the ones that don't.

        Leases of other controllers are only reported (status shows them as stale or lease_idle).
        """
        obs = await self.observe(fresh=True)
        backed = {p.get("lease") for p in obs.get("profiles", {}).values() if p.get("lease")}
        # A load still waiting for its turn on the rig has a job but no rig profile yet.
        queued = {j["job"].removeprefix(JOB_PREFIX) for j in obs.get("jobs", [])
                  if j.get("active_state") in ("active", "activating")}
        now = datetime.now(timezone.utc)
        renewed, released = [], []
        for lease in obs.get("leases", []):
            if lease.get("owner") != OWNER:
                continue
            lid = lease["lease_id"]
            if lid in backed or str(lease.get("run_id", "")).removeprefix("profile-") in queued:
                if not is_expired(lease, now):
                    await self.renew(lid, OWNER, INTERACTIVE_TTL_S)
                    renewed.append(lid)
                continue
            acquired = datetime.fromisoformat(str(lease["acquired_at"]).replace("Z", "+00:00"))
            if (now - acquired).total_seconds() < ORPHAN_GRACE_S:
                continue
            await self.record.release(lid, OWNER, force=True)
            released.append(lid)
            log.info("reconcile: released %s (no rig load or runtime behind it)", lid)
        if renewed or released:
            self._obs = None
        return {"renewed": renewed, "released": released}

    async def run_reconciler(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:  # keep reconciling; the next pass may succeed
                log.warning("reconcile failed: %s", exc)
            await asyncio.sleep(RECONCILE_EVERY_S)
