"""The rig manager: controller management (docs/MANAGER.md).

It decides who holds which GPUs and what the rig does next. It keeps no state of its own: the lease
and claim record, the active-controller record and the plan queue all live on the rig
(store.RigRecord), and the rig works through the plan by itself (rig-plan.service) and enforces the
hardware rules (one load at a time, health, power, thermal, RAM). So the rig keeps going if this
controller goes away, and this or another controller picks up from `rig observe`.

Shared report (rule 1c): the rig keeps the record and the plan; this controller keeps a copy on its own
disk, saved on every observation. When the rig reboots it holds its plan; the reconciler reviews it
(restoring the plan from the copy if the rig's is behind) and resumes it. When the rig is unreachable,
status answers from the copy and says since when.

One controller is active at a time. This one takes control when there is none or the active one's
heartbeat is stale, heartbeats while it holds it, and is read-only (writes refused with 409) while
another controller is active. A handoff is an explicit takeover (`take_control(force=True)`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from ..rig import Rig, RigUnavailable
from . import fit, memory, models, placement
from .errors import ManagerError
from .status import gpu_rows, is_expired
from .store import RigRecord

log = logging.getLogger("llm_gateway.manager")

OWNER = "llm-gateway"  # owner of the leases the manager takes for interactive profiles
INTERACTIVE_TTL_S = 300
RECONCILE_EVERY_S = 30
OBSERVE_CACHE_S = 3.0
LOAD_POLL_S = 3.0
DONE_NOT_READY_CHECKS = 3  # observations of a done load before it counts as not serving
# A lease of ours with no rig profile or plan item behind it is released after this long (covers the
# moment between taking the lease and the rig recording the load).
ORPHAN_GRACE_S = 60
# A split load that waits for its cards reserves them with a claim this long, renewed while it waits
# (docs/MANAGER.md, "Reservations"): if this controller goes away mid-wait, the reservation lapses.
WAIT_CLAIM_TTL_S = 180
WAIT_POLL_S = 10.0


def _limit(wait_s: float) -> str:
    return "with no time limit" if wait_s < 0 else f"up to {wait_s:.0f} s"


class Manager:
    def __init__(self, rig: Rig, start_timeout_s: float = 900, controller_id: str = "controller",
                 mirror_path: Path | None = None):
        self.rig = rig
        self.controller_id = controller_id
        self.record = RigRecord(rig, controller_id)
        self.start_timeout_s = start_timeout_s
        self.in_control = False
        self.control_note = "not claimed yet"
        self._obs: dict | None = None
        self._obs_at = 0.0
        self._obs_lock = asyncio.Lock()
        # This controller makes one placement decision at a time; the rig refuses overlaps across controllers.
        self._place_lock = asyncio.Lock()
        # Leases a load or unload of this manager is working on right now. The reconciler leaves them alone
        # for the whole operation: between taking a lease and the rig recording the profile, and between the
        # rig unloading a profile and this manager releasing its lease, the lease looks unbacked.
        self._in_flight: set[str] = set()
        # This controller's copy of the rig's report, and what it has seen happen to the rig.
        self.mirror_path = mirror_path
        self.mirror: dict | None = self._load_mirror()
        self.rig_down_since: float | None = None
        self.events: deque[dict] = deque(maxlen=50)

    # ------------------------------------------------------------------ control

    async def take_control(self, force: bool = False) -> bool:
        """Become the rig's active controller (force: explicit handoff from another one)."""
        try:
            ctl = await self.record.claim_control(force)
        except ManagerError as exc:
            if exc.extra.get("rig_kind") != "not_controller":
                raise
            self.in_control, self.control_note = False, str(exc)
            return False
        self.in_control = True
        self.control_note = f"active since {ctl.get('since')}" + (
            f" (took over from {ctl['took_over_from']})" if ctl.get("took_over_from") else "")
        log.info("controller %s: %s", self.controller_id, self.control_note)
        self._obs = None
        return True

    async def release_control(self) -> None:
        if self.in_control:
            await self.record.release_control()
            self.in_control, self.control_note = False, "released"

    async def _require_control(self) -> None:
        if self.in_control or await self.take_control():
            return
        raise ManagerError(409, f"read-only: {self.control_note}. This controller is {self.controller_id!r}; "
                                "hand off with POST /v1/manager/controller/claim {\"force\": true}")

    def control(self, obs: dict | None = None) -> dict:
        active = ((obs or self._obs or {}).get("plan") or {}).get("controller")
        return {"this": self.controller_id, "in_control": self.in_control, "note": self.control_note,
                "active": active}

    # ------------------------------------------------------------------ observation

    async def observe(self, fresh: bool = False) -> dict:
        async with self._obs_lock:
            if fresh or self._obs is None or time.monotonic() - self._obs_at > OBSERVE_CACHE_S:
                try:
                    obs = await self.rig.observe()
                except RigUnavailable as exc:
                    if self.rig_down_since is None:
                        self.rig_down_since = time.time()
                        self._event(f"rig unreachable: {str(exc)[:200]}")
                    raise
                if self.rig_down_since is not None:
                    self._event(f"rig back after {time.time() - self.rig_down_since:.0f} s")
                    self.rig_down_since = None
                self._note_boot(obs)
                self._save_mirror(obs)
                self._obs = obs
                self._obs_at = time.monotonic()
            return self._obs

    # ------------------------------------------------------------------ the copy of the rig's report

    def _event(self, text: str) -> None:
        log.warning("rig: %s", text)
        self.events.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": text})

    def _load_mirror(self) -> dict | None:
        if not self.mirror_path or not self.mirror_path.is_file():
            return None
        try:
            return json.loads(self.mirror_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("rig report copy unreadable (%s); starting without it", exc)
            return None

    def mirror_plan(self) -> dict:
        return (self.mirror or {}).get("plan") or {}

    def _note_boot(self, obs: dict) -> None:
        new = (obs.get("plan") or {}).get("boot_id")
        old = self.mirror_plan().get("boot_id")
        if new and old and new != old:
            self._event(f"rig rebooted (booted {(obs.get('plan') or {}).get('booted_at')})")

    def _save_mirror(self, obs: dict) -> None:
        """Keep the latest report, except the plan when the rig's copy is behind ours (it lost its file):
        then ours is kept until the reconciler has written it back."""
        plan = obs.get("plan") or {}
        keep = plan
        mine = self.mirror_plan()
        if int(mine.get("version") or 0) > int(plan.get("version") or 0):
            keep = mine
        self.mirror = {"saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "observed_at": obs.get("observed_at"), "rig_plan_version": plan.get("version"),
                       "plan": keep, **{k: obs.get(k) for k in ("mode", "gpus", "profiles", "leases", "claims",
                                                                "guard", "containers")}}
        if not self.mirror_path:
            return
        try:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.mirror_path.with_name(f".{self.mirror_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.mirror, indent=1) + "\n", encoding="utf-8")
            tmp.replace(self.mirror_path)
        except OSError as exc:
            log.warning("could not save the rig report copy: %s", exc)

    async def settle_hold(self, obs: dict) -> dict | None:
        """The rig rebooted (or lost its plan) and holds its queue: write back our copy if the rig's is
        behind, report what was interrupted, and resume. Interrupted loads are not retried: their callers
        got the failure, and their leases are released below as unbacked."""
        plan = obs.get("plan") or {}
        hold = plan.get("hold")
        if not hold:
            return None
        mine = self.mirror_plan()
        restored = None
        if int(mine.get("version") or 0) > int(plan.get("version") or 0):
            restored = await self.record.plan_restore(mine)
            self._event(f"rig plan was behind (version {plan.get('version')} < {mine.get('version')}); "
                        f"wrote back this controller's copy")
        items = {i["id"]: i for i in (restored or plan).get("items", [])}
        stopped = [f"{iid}: {items.get(iid, {}).get('result') or 'interrupted'}" for iid in hold.get("interrupted", [])]
        await self.record.plan_resume()
        self._obs = None
        self._event(f"plan held ({hold.get('reason')}) since {hold.get('since')}; resumed"
                    + (f"; interrupted, not retried: {'; '.join(stopped)}" if stopped else "; nothing interrupted"))
        return {"hold": hold, "restored": bool(restored), "interrupted": stopped}

    def rig_report(self) -> dict:
        """Reachability and recent events, for status."""
        return {"reachable": self.rig_down_since is None,
                "down_since": (datetime.fromtimestamp(self.rig_down_since, timezone.utc).isoformat(timespec="seconds")
                               if self.rig_down_since else None),
                "copy_saved_at": (self.mirror or {}).get("saved_at"), "events": list(self.events)[-10:]}

    async def leases(self) -> list[dict]:
        return (await self.observe(fresh=True)).get("leases", [])

    async def claims(self) -> list[dict]:
        return (await self.observe(fresh=True)).get("claims", [])

    async def plan(self) -> dict:
        return (await self.observe(fresh=True)).get("plan") or {}

    async def status(self) -> dict:
        """The single report: what the rig is doing now (GPUs, profiles, guard) and next (plan). With the
        rig unreachable, the last copy this controller kept, marked as such."""
        try:
            obs = await self.observe()
        except RigUnavailable:
            if not self.mirror:
                raise
            m = self.mirror
            return {"observed_at": m.get("observed_at"), "mode": m.get("mode"), "summary": {"rig": "unreachable"},
                    "controller": self.control(), "guard": m.get("guard"), "gpus": [], "profiles": m.get("profiles", {}),
                    "plan": m.get("plan") or {}, "jobs": [], "leases": m.get("leases", []),
                    "claims": m.get("claims", []), "rig": self.rig_report(), "from_copy": True}
        leases, claims = obs.get("leases", []), obs.get("claims", [])
        rows = gpu_rows(obs, leases, claims)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        return {"observed_at": obs.get("observed_at"), "mode": obs.get("mode"), "summary": counts,
                "controller": self.control(obs), "guard": obs.get("guard"), "gpus": rows,
                "profiles": obs.get("profiles", {}), "plan": obs.get("plan") or {}, "jobs": obs.get("jobs", []),
                "last_error": obs.get("last_error"), "state_error": obs.get("state_error"),
                "leases": leases, "claims": claims, "rig": self.rig_report()}

    # ------------------------------------------------------------------ leases

    async def acquire(self, *, controller: str, owner: str, run_id: str, gpus_needed: list[dict] | None = None,
                      gpu_ids: list[int] | None = None, claim: str | None = None, ttl_s: int = 120,
                      model: str = "", purpose: str = "", lane: str | None = None) -> dict:
        """`controller` is the lease's controller type (interactive, benchmarks, plans, ...)."""
        await self._require_control()
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
                "metadata": {"port": port, "model": model, "purpose": purpose, "claim": claim,
                             "by_controller": self.controller_id}})
            self._obs = None  # the record changed
        log.info("lease %s %s/%s GPU %s :%s %s", lease["lease_id"], controller, owner, gpus, port, model)
        return lease

    async def renew(self, lease_id: str, owner: str, ttl_s: int = 120) -> dict:
        await self._require_control()
        return await self.record.renew(lease_id, owner, ttl_s)

    async def release(self, lease_id: str, owner: str = "", force: bool = False) -> None:
        """Release a lease. Unless forced, its runtime must be gone (nothing on its GPUs)."""
        await self._require_control()
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

    async def add_claim(self, name: str, gpu_ids: list[int], note: str = "", by: str = "", *, owner: str = "",
                        ttl_s: int | None = None, purpose: str = "", model: str = "") -> dict:
        """Take cards out of the pool. They may still be leased: the claim then reserves them for when
        their holder releases (how a split waits for a matching set). ttl_s: lapses unless renewed."""
        await self._require_control()
        claim = await self.record.add_claim(name, gpu_ids, note, by, owner=owner, ttl_s=ttl_s, purpose=purpose,
                                            model=model)
        self._obs = None
        return claim

    async def extend_claim(self, name: str, gpu_ids: list[int], owner: str = "") -> dict:
        await self._require_control()
        claim = await self.record.extend_claim(name, gpu_ids, owner)
        self._obs = None
        return claim

    async def renew_claim(self, name: str, owner: str = "", ttl_s: int | None = None) -> dict:
        await self._require_control()
        return await self.record.renew_claim(name, owner, ttl_s)

    async def remove_claim(self, name: str) -> dict:
        await self._require_control()
        claim = await self.record.remove_claim(name)
        self._obs = None
        return claim

    # ------------------------------------------------------------------ operator overrides

    async def kill(self, gpu_ids: list[int], reason: str = "") -> dict:
        """Stop every runtime on these cards, release every lease holding any of them, and drop every
        claim on them; then reconcile, which clears the profiles whose runtimes went."""
        await self._require_control()
        gpus = sorted({int(g) for g in gpu_ids})
        out = await self.record.kill(gpus, reason)
        self._obs = None
        self._event(f"kill GPU {gpus}" + (f" ({reason})" if reason else "") + f": stopped {out.get('stopped')}, "
                    f"released {out.get('released')}, claims dropped {out.get('claims_dropped')}")
        await self.reconcile_once()
        return out

    # ------------------------------------------------------------------ reservations (split waits)

    async def _wait_for_cards(self, gpus: list[int], claim: str | None, wait_s: float, model: str,
                              pid: str) -> tuple[str | None, str | None]:
        """Wait until every card in `gpus` is free (for the claim, when one is named), reserving them
        meanwhile so single work cannot take them as they free. Returns (claim to load under, the claim
        this wait made, or None). wait_s < 0: no limit. Past wait_s: the reservation is dropped and
        ManagerError 504. Removing the claim the wait holds (or the caller's named claim) cancels it: 409."""

        def state(obs: dict, name: str | None) -> tuple[bool, dict]:
            try:
                free, why = placement.availability(obs, obs.get("leases", []), obs.get("claims", []), name)
            except KeyError:
                return False, {g: f"claim {name} is gone" for g in gpus}
            ids = {f["index"] for f in free}
            return all(g in ids for g in gpus), {g: why.get(g) for g in gpus if g not in ids}

        obs = await self.observe(fresh=True)
        if claim:
            named = next((c for c in obs.get("claims", []) if c["name"] == claim), None)
            if named and not set(gpus) <= set(named["gpu_ids"]):
                raise ManagerError(409, f"claim {claim} covers GPU {named['gpu_ids']}, not all of {gpus}")
        ready, why = state(obs, claim)
        if ready:
            return claim, None
        own = None
        if not claim:
            name = f"wait-{pid}"[:64]
            try:
                await self.add_claim(name, gpus, note=f"waiting to load {model}", by=self.controller_id, owner=OWNER,
                                     ttl_s=WAIT_CLAIM_TTL_S, purpose=f"split wait for {pid}", model=model)
                own = claim = name
            except ManagerError as exc:
                # Another claim covers some card: wait without a reservation of our own until it goes.
                self._event(f"{pid}: cannot reserve GPU {gpus} ({exc}); waiting without a reservation")
        self._event(f"{pid}: waiting {_limit(wait_s)} for GPU {gpus}"
                    + (f" (reserved as {own})" if own else "") + f": {why}")
        deadline = time.monotonic() + wait_s if wait_s >= 0 else float("inf")
        renewed = time.monotonic()
        while True:
            await asyncio.sleep(WAIT_POLL_S)
            obs = await self.observe(fresh=True)
            ready, why = state(obs, claim)
            if ready:
                self._event(f"{pid}: GPU {gpus} free; loading")
                return claim, own
            if claim and not any(c["name"] == claim for c in obs.get("claims", [])):
                # The reservation went (an operator removed it, or the caller cancelled): stop waiting.
                self._event(f"{pid}: wait cancelled, claim {claim} was removed")
                raise ManagerError(409, f"wait for {pid} cancelled: claim {claim} was removed")
            if time.monotonic() > deadline:
                if own:
                    try:
                        await self.remove_claim(own)
                    except ManagerError:
                        pass
                raise ManagerError(504, f"GPU {gpus} not free after waiting {wait_s:.0f} s for {pid}: "
                                        + "; ".join(f"GPU {g} {w}" for g, w in why.items()), unavailable=why)
            if own and time.monotonic() - renewed > WAIT_CLAIM_TTL_S / 3:
                try:
                    await self.renew_claim(own, OWNER, WAIT_CLAIM_TTL_S)
                    renewed = time.monotonic()
                except ManagerError:
                    pass  # gone: the next pass sees it and cancels

    # ------------------------------------------------------------------ plan

    async def cancel_item(self, item_id: str) -> dict:
        await self._require_control()
        item = await self.record.plan_cancel(item_id)
        self._obs = None
        return item

    async def _wait_item(self, item_id: str, profile_id: str, until_ready: bool) -> dict:
        """Poll the rig until the plan item finishes (or, for a load, the profile serves)."""
        deadline = time.monotonic() + self.start_timeout_s
        not_ready = 0
        while True:
            obs = await self.observe(fresh=True)
            item = next((i for i in (obs.get("plan") or {}).get("items", []) if i["id"] == item_id), None)
            prof = obs.get("profiles", {}).get(profile_id)
            if until_ready and prof and prof.get("state") == "ready":
                return item or {}
            if item is None:
                raise RigUnavailable(502, f"plan item {item_id} disappeared from the rig")
            if item["state"] in ("done", "failed", "cancelled") and not (until_ready and item["state"] == "done"):
                return item
            if until_ready and item["state"] == "done":
                # Done but not ready: look again before calling it failed (failing releases the lease,
                # which stops the runtime). A rig whose observe reads the profiles before the plan can
                # report the item done while the profile still reads loading.
                not_ready += 1
                if not_ready < DONE_NOT_READY_CHECKS:
                    await asyncio.sleep(2)
                    continue
                # Still not ready: the runtime went away right after loading (rig guard, crash).
                return {**item, "state": "failed", "result": f"{profile_id} loaded but is not serving "
                                                              f"(rig guard: {(obs.get('guard') or {}).get('why')})"}
            if time.monotonic() > deadline:
                raise RigUnavailable(504, f"{profile_id}: plan item {item_id} still {item['state']} after "
                                          f"{self.start_timeout_s:.0f}s; the rig keeps working on it, retry shortly")
            await asyncio.sleep(LOAD_POLL_S)

    # ------------------------------------------------------------------ interactive profiles

    async def load_profile(self, profile_id: str, profile: dict, claim: str | None = None) -> None:
        """Lease cards for a profile, queue the load in the rig's plan, and wait for it to serve.

        The rig works through its plan by itself; past the start timeout the caller gets a 504 and the
        load keeps going on the rig, with its lease.
        """
        use = profile.get("use", "chat")
        guard = (await self.observe(fresh=True)).get("guard") or {}
        cache_mib = memory.cache_ram_mib(use, guard.get("mem_available_mb"))
        lease = await self.acquire(controller="interactive", owner=OWNER, run_id=f"profile-{profile_id}",
                                   gpus_needed=profile["gpus_needed"], claim=claim, ttl_s=INTERACTIVE_TTL_S,
                                   model=profile.get("model", ""),
                                   purpose=f"gateway profile {profile_id} ({use}, cache {cache_mib} MiB)")
        lease_id = lease["lease_id"]
        self._in_flight.add(lease_id)
        try:
            try:
                item = await self.record.plan_add("load", {
                    "profile": profile_id, "gpus": lease["gpu_ids"], "port": lease["metadata"]["port"],
                    "lease": lease_id, "cache_ram": cache_mib})
            except (ManagerError, RigUnavailable):
                await self.release(lease_id, OWNER, force=True)
                raise
            log.info("load %s queued in the rig plan as %s (use %s, cache %s MiB)", profile_id, item["id"], use,
                     cache_mib)
            done = await self._wait_item(item["id"], profile_id, until_ready=True)
            if done.get("state") in ("failed", "cancelled"):
                obs = await self.observe(fresh=True)
                if profile_id in obs.get("profiles", {}):
                    await self.rig.stop_profile(profile_id)
                await self.release(lease_id, OWNER, force=True)
                raise RigUnavailable(502, f"rig did not load {profile_id} ({done['state']}): "
                                          f"{str(done.get('result') or '')[-1500:]}")
        finally:
            self._in_flight.discard(lease_id)

    async def unload_profile(self, profile_id: str) -> dict:
        await self._require_control()
        obs = await self.observe(fresh=True)
        prof = obs.get("profiles", {}).get(profile_id)
        if not prof:
            raise ManagerError(404, f"{profile_id} is not loaded")
        lease_id = prof.get("lease")
        if lease_id:
            self._in_flight.add(lease_id)
        try:
            item = await self.record.plan_add("unload", {"profile": profile_id})
            done = await self._wait_item(item["id"], profile_id, until_ready=False)
            released = None
            if lease_id and any(l.get("lease_id") == lease_id for l in obs.get("leases", [])):
                try:
                    await self.release(lease_id, OWNER, force=True)
                except ManagerError as exc:
                    if exc.status != 404:  # already gone (released by an operator or another controller)
                        raise
                released = lease_id
        finally:
            if lease_id:
                self._in_flight.discard(lease_id)
        return {"unloaded": profile_id, "item": item["id"], "state": done["state"], "released": released}

    # ------------------------------------------------------------------ model loads (any model, any cards)

    async def _shape(self, model: str, use: str, profile: str | None, ctx: int | None, ctx_min: int,
                     margin_mb: int) -> tuple[dict, Any, dict]:
        """(registry entry, GGUF shape, plan options) for a model: what every fit is planned from."""
        if use not in memory.USES:
            raise ManagerError(400, f"use must be one of {memory.USES}")
        try:
            r = models.resolve(model, use, profile)
        except (KeyError, FileNotFoundError) as exc:
            raise ManagerError(404, str(exc.args[0] if exc.args else exc)) from exc
        try:
            shape = await asyncio.to_thread(fit.shape_for, models.local_path(r["gguf_path"]), r["model"])
        except fit.FitError as exc:
            raise ManagerError(404, str(exc)) from exc
        rt = r.get("runtime", {})
        kw = {"ctx": ctx, "ctx_min": ctx_min, "ubatch": min(int(rt.get("batch_size", 64)), 512),
              "parallel": int(rt.get("parallel", 1)), "margin_mb": margin_mb}
        return r, shape, kw

    async def fit_model(self, model: str, *, cards: int | None = None, gpu_ids: list[int] | None = None,
                        ctx: int | None = None, ctx_min: int = fit.CTX_MIN_DEFAULT, use: str = "benchmark",
                        profile: str | None = None, claim: str | None = None,
                        margin_mb: int = fit.MARGIN_MB_DEFAULT) -> dict:
        """The harness's answer to "load `model` on `cards` cards (or on `gpu_ids`, or, with neither, on the
        fewest free cards that give `ctx` / at least `ctx_min`)": will it fit, at what context and split.
        Nothing is leased or sent to the rig. ManagerError 422 when it will not fit."""
        if cards and gpu_ids:
            raise ManagerError(400, "give cards (a count) or gpu_ids, not both")
        r, shape, kw = await self._shape(model, use, profile, ctx, ctx_min, margin_mb)
        obs = await self.observe(fresh=True)
        try:
            free, why = placement.availability(obs, obs.get("leases", []), obs.get("claims", []), claim)
        except KeyError as exc:
            raise ManagerError(404, str(exc.args[0])) from exc
        if gpu_ids:
            wanted = sorted({int(g) for g in gpu_ids})
            blocked = {g: why.get(g, "unknown GPU") for g in wanted if g not in {f["index"] for f in free}}
            if blocked:
                raise ManagerError(409, "GPU(s) not available: " + "; ".join(f"GPU {g} {w}" for g, w in blocked.items()),
                                   unavailable=blocked)
            chosen = [f for f in free if f["index"] in wanted]
        elif cards:
            if cards > len(free):
                raise ManagerError(503, f"{cards} card(s) asked, {len(free)} free: {[f['index'] for f in free]}",
                                   unavailable=why)
            chosen = fit.choose_cards(free, int(cards))
        else:
            chosen = None
        if chosen is None:
            result = fit.choose(shape, free, **kw) or fit.plan(shape, fit.choose_cards(free, len(free)), **kw)
            result["chosen_by"] = "harness: fewest free cards that fit"
        else:
            result = fit.plan(shape, chosen, **kw)
        result.update(registry_profile=r["profile"], status=r["status"], use=use, gguf_path=r["gguf_path"])
        if not result["fits"]:
            alts = fit.suggest(shape, free, **kw)
            msg = result["reason"] + (". Fits on: " + "; ".join(alts) if alts else
                                      ". No combination of the free cards holds it.")
            raise ManagerError(422, msg, fit=result, suggestions=alts)
        return result

    async def _collect_cards(self, model: str, wait_s: float, use: str, **fit_kw) -> tuple[str, list[int]]:
        """For a load that names no cards: the cards the harness would choose on an idle rig say how many
        cards of which size the model needs; claim free cards of those sizes one at a time as they free (one
        claim, extended), so single work cannot keep taking them, until the claim holds the set. Returns
        (the claim, its cards). wait_s < 0: no limit. Removing the claim cancels the wait (409); past wait_s
        the claim is dropped and 504."""
        r, shape, kw = await self._shape(model, use, fit_kw.get("profile"), fit_kw.get("ctx"),
                                         fit_kw.get("ctx_min", fit.CTX_MIN_DEFAULT),
                                         fit_kw.get("margin_mb", fit.MARGIN_MB_DEFAULT))
        obs = await self.observe(fresh=True)
        # Planned on empty cards: a busy card reports the memory its current model uses, which would make
        # the set look bigger than the model needs once those cards free.
        target = fit.choose(shape, [{**g, "used_mb": 0} for g in obs.get("gpus", [])], **kw)
        if target is None:
            raise ManagerError(422, f"{model} won't fit on this rig even with every card free")
        size = {g["index"]: int(g["total_mb"]) for g in obs.get("gpus", [])}
        need = Counter(size[g] for g in target["gpus"])
        slug = re.sub(r"[^a-z0-9]+", "-", r["model"].lower()).strip("-")
        name = f"wait-{slug}-{uuid.uuid4().hex[:6]}"[:64]
        wanted = " + ".join(f"{n} x {mb} MiB" for mb, n in sorted(need.items()))
        self._event(f"{r['model']}: collecting {wanted} cards as they free, {_limit(wait_s)}, as claim {name}")
        held: list[int] = []
        deadline = time.monotonic() + wait_s if wait_s >= 0 else float("inf")
        renewed = time.monotonic()
        try:
            while True:
                claims = obs.get("claims", [])
                if held and not any(c["name"] == name for c in claims):
                    self._event(f"{r['model']}: collecting cancelled, claim {name} was removed")
                    raise ManagerError(409, f"wait for {r['model']} cancelled: claim {name} was removed")
                free, _ = placement.availability(obs, obs.get("leases", []), claims, None)
                have = Counter(size[g] for g in held)
                take = []
                for g in sorted(free, key=lambda g: g["index"]):
                    mb = int(g["total_mb"])
                    if have[mb] + sum(1 for t in take if size[t] == mb) < need[mb]:
                        take.append(g["index"])
                if take:
                    if held:
                        await self.extend_claim(name, take, OWNER)
                    else:
                        await self.add_claim(name, take, note=f"collecting cards to load {r['model']}",
                                             by=self.controller_id, owner=OWNER, ttl_s=WAIT_CLAIM_TTL_S,
                                             purpose=f"collect {wanted} for {r['model']}", model=r["model"])
                    held = sorted(held + take)
                    renewed = time.monotonic()
                    self._event(f"{r['model']}: claimed GPU {take}, holding {held} of {wanted}")
                if Counter(size[g] for g in held) == need:
                    if take:
                        obs = await self.observe(fresh=True)
                    try:
                        ready, _ = placement.availability(obs, obs.get("leases", []), obs.get("claims", []), name)
                    except KeyError:
                        raise ManagerError(409, f"wait for {r['model']} cancelled: claim {name} was removed")
                    if {g["index"] for g in ready} >= set(held):
                        self._event(f"{r['model']}: holding GPU {held}; loading")
                        return name, held
                if time.monotonic() > deadline:
                    raise ManagerError(504, f"{r['model']}: only GPU {held} of {wanted} free after "
                                            f"{wait_s:.0f} s")
                if held and time.monotonic() - renewed > WAIT_CLAIM_TTL_S / 3:
                    try:
                        await self.renew_claim(name, OWNER, WAIT_CLAIM_TTL_S)
                        renewed = time.monotonic()
                    except ManagerError:
                        pass  # gone: the next pass sees it and cancels
                await asyncio.sleep(WAIT_POLL_S)
                obs = await self.observe(fresh=True)
        except BaseException:
            if held:
                try:
                    await self.remove_claim(name)
                except ManagerError:
                    pass
            raise

    async def load_model(self, model: str, *, extra_args: list[str] | None = None, claim: str | None = None,
                         use: str = "benchmark", wait_s: float = 0, force: bool = False, **fit_kw) -> dict:
        """Fit, lease, queue the load in the rig's plan, wait until it serves, and report what the rig
        actually loaded (context, split, memory per card). The rig re-checks with llama.cpp's own
        projection and refuses a load that would not fit.

        wait_s: when the named cards (gpu_ids) are not all free, reserve them and wait up to this long
        (docs/MANAGER.md, "Reservations"); with no cards named, collect the cards the model needs as they
        free (`_collect_cards`); with a card count instead, retry the fit until it succeeds. wait_s < 0
        waits with no limit; removing the wait's claim cancels it.
        force: operator override, kill whatever runs on the named cards first."""
        await self._require_control()
        gpu_ids = sorted({int(g) for g in fit_kw.get("gpu_ids") or []})
        if force:
            if not gpu_ids:
                raise ManagerError(400, "force needs explicit gpu_ids")
            await self.kill(gpu_ids, reason=f"force load {model}")
        own = None
        try:
            if gpu_ids and wait_s != 0:
                r0 = models.resolve(model, use, fit_kw.get("profile"))
                claim, own = await self._wait_for_cards(gpu_ids, claim, wait_s, model,
                                                        models.profile_id(r0["model"], gpu_ids))
                plan_ = await self.fit_model(model, claim=claim, use=use, **fit_kw)
            elif wait_s != 0 and not fit_kw.get("cards"):
                claim, gpus = await self._collect_cards(model, wait_s, use, **fit_kw)
                own = claim
                fit_kw = {**fit_kw, "gpu_ids": gpus}
                plan_ = await self.fit_model(model, claim=claim, use=use, **fit_kw)
            elif wait_s != 0:
                deadline = time.monotonic() + wait_s if wait_s > 0 else float("inf")
                while True:
                    try:
                        plan_ = await self.fit_model(model, claim=claim, use=use, **fit_kw)
                        break
                    except ManagerError as exc:
                        if exc.status not in (409, 503) or time.monotonic() > deadline:
                            raise
                    await asyncio.sleep(WAIT_POLL_S)
            else:
                plan_ = await self.fit_model(model, claim=claim, use=use, **fit_kw)
            return await self._load_planned(model, plan_, extra_args=extra_args, claim=claim, use=use, **fit_kw)
        finally:
            if own:  # the lease holds the cards now (or the load failed): the wait's reservation goes
                try:
                    await self.remove_claim(own)
                except ManagerError:
                    pass

    async def _load_planned(self, model: str, plan_: dict, *, extra_args: list[str] | None, claim: str | None,
                            use: str, **fit_kw) -> dict:
        r = models.resolve(model, use, fit_kw.get("profile"))
        rt = r.get("runtime", {})
        gpus = plan_["gpus"]
        pid = models.profile_id(r["model"], gpus)
        spec = {"model": r["model"], "gguf_path": r["gguf_path"], "ctx_size": plan_["ctx"],
                "batch_size": int(rt.get("batch_size", 64)), "parallel": int(rt.get("parallel", 1)),
                "n_gpu_layers": "all", "extra_args": [*r.get("extra_args", []), *(extra_args or [])],
                "image": r.get("image"), "use": use, "registry_profile": r["profile"],
                **({"tensor_split": plan_["tensor_split"]} if plan_.get("tensor_split") else {})}
        guard = (await self.observe(fresh=True)).get("guard") or {}
        cache_mib = memory.cache_ram_mib(use, guard.get("mem_available_mb"))
        # Owner llm-gateway, like profile loads: the reconciler renews it while the rig profile lives.
        lease = await self.acquire(controller=models.CONTROLLER_BY_USE.get(use, "interactive"), owner=OWNER,
                                   run_id=f"model-{pid}", gpu_ids=gpus, claim=claim, ttl_s=INTERACTIVE_TTL_S,
                                   model=r["model"], purpose=f"model load {pid} ({use}, ctx {plan_['ctx']})")
        lease_id = lease["lease_id"]
        self._in_flight.add(lease_id)
        try:
            try:
                item = await self.record.plan_add("load", {
                    "profile": pid, "spec": spec, "gpus": gpus, "port": lease["metadata"]["port"],
                    "lease": lease_id, "cache_ram": cache_mib})
            except (ManagerError, RigUnavailable):
                await self.release(lease_id, OWNER, force=True)
                raise
            log.info("model load %s queued as %s (GPU %s ctx %s split %s)", pid, item["id"], gpus, plan_["ctx"],
                     plan_.get("tensor_split"))
            done = await self._wait_item(item["id"], pid, until_ready=True)
            obs = await self.observe(fresh=True)
            if done.get("state") in ("failed", "cancelled"):
                if pid in obs.get("profiles", {}):
                    await self.rig.stop_profile(pid)
                await self.release(lease_id, OWNER, force=True)
                raise RigUnavailable(502, f"rig did not load {pid} ({done['state']}): "
                                          f"{str(done.get('result') or '')[-1500:]}")
        finally:
            self._in_flight.discard(lease_id)
        loaded = obs.get("profiles", {}).get(pid, {})
        return {"profile": pid, "model": r["model"], "state": "ready", "port": lease["metadata"]["port"],
                "gpus": gpus, "lease": lease_id, "ctx": loaded.get("ctx_size", plan_["ctx"]),
                "tensor_split": plan_.get("tensor_split"), "estimate": plan_, "measured": loaded.get("fit")}

    # ------------------------------------------------------------------ reconcile

    async def reconcile_once(self) -> dict:
        """Keep control (heartbeat, or take it when the active controller is gone), then renew our
        leases that still have a load, runtime or queued plan item behind them and release the rest.

        Leases of other controllers are only reported (status shows them as stale or lease_idle).
        """
        if self.in_control:
            try:
                await self.record.heartbeat()
            except ManagerError as exc:
                if exc.extra.get("rig_kind") != "not_controller":
                    raise
                self.in_control, self.control_note = False, f"lost control: {exc}"
                log.warning("controller %s: %s", self.controller_id, self.control_note)
        else:
            await self.take_control()
        if not self.in_control:
            return {"in_control": False, "note": self.control_note, "renewed": [], "released": []}

        obs = await self.observe(fresh=True)
        if (obs.get("plan") or {}).get("hold"):
            await self.settle_hold(obs)
            obs = await self.observe(fresh=True)
        # A profile whose runtime is gone (the rig guard freed RAM, or it crashed): clear it on the rig;
        # its lease is then unbacked and released below.
        for pid, prof in obs.get("profiles", {}).items():
            if prof.get("state") == "gone":
                log.info("reconcile: %s runtime is gone on the rig; clearing it", pid)
                await self.rig.stop_profile(pid)
        backed = {p.get("lease") for p in obs.get("profiles", {}).values()
                  if p.get("lease") and p.get("state") != "gone"}
        # A load still waiting in the rig's plan has a lease but no rig profile yet.
        backed |= {i["args"].get("lease") for i in (obs.get("plan") or {}).get("next", []) if i["kind"] == "load"}
        now = datetime.now(timezone.utc)
        renewed, released = [], []
        for lease in obs.get("leases", []):
            if lease.get("owner") != OWNER:
                continue
            lid = lease["lease_id"]
            if lid in self._in_flight:
                continue  # a load or unload of ours is working on it; it settles the lease itself
            if lid in backed:
                if not is_expired(lease, now):
                    await self.record.renew(lid, OWNER, INTERACTIVE_TTL_S)
                    renewed.append(lid)
                continue
            acquired = datetime.fromisoformat(str(lease["acquired_at"]).replace("Z", "+00:00"))
            if (now - acquired).total_seconds() < ORPHAN_GRACE_S:
                continue
            await self.record.release(lid, OWNER, force=True)
            released.append(lid)
            log.info("reconcile: released %s (no rig load, runtime or plan item behind it)", lid)
        if renewed or released:
            self._obs = None
        return {"in_control": True, "renewed": renewed, "released": released}

    async def run_reconciler(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:  # keep reconciling; the next pass may succeed
                log.warning("reconcile failed: %s", exc)
            await asyncio.sleep(RECONCILE_EVERY_S)
