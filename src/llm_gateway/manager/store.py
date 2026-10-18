"""The lease and claim record, kept by the rig (`rig lease ...`, `rig claim ...`, rig-side hw_state.py).

The controller holds no allocation state of its own: it reads the record from `rig observe` and asks
the rig to change it. The rig is the single writer, so the record survives a lost network and any
controller (the Pi today) can pick up where another left off.
"""

from __future__ import annotations

import json
from typing import Any

from ..rig import Rig, RigUnavailable
from .errors import KIND_STATUS, ManagerError


class RigRecord:
    def __init__(self, rig: Rig):
        self.rig = rig

    async def _call(self, *args: str) -> dict[str, Any]:
        code, out = await self.rig._run(*args, timeout=60)
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RigUnavailable(502, f"rig {' '.join(args[:2])} returned no JSON: {out[-300:]}") from exc
        if code != 0:
            raise ManagerError(KIND_STATUS.get(payload.get("kind"), 502), payload.get("error", out[-300:]))
        return payload

    async def acquire(self, spec: dict[str, Any]) -> dict[str, Any]:
        return (await self._call("lease", "acquire", "--spec", json.dumps(spec)))["lease"]

    async def renew(self, lease_id: str, owner: str, ttl_s: int) -> dict[str, Any]:
        return (await self._call("lease", "renew", lease_id, "--owner", owner, "--ttl", str(int(ttl_s))))["lease"]

    async def release(self, lease_id: str, owner: str = "", force: bool = False) -> None:
        args = ["lease", "release", lease_id, "--owner", owner] + (["--force"] if force else [])
        await self._call(*args)

    async def add_claim(self, name: str, gpu_ids: list[int], note: str = "", by: str = "") -> dict[str, Any]:
        args = ["claim", "add", name, "--gpus", ",".join(str(int(g)) for g in gpu_ids), "--note", note]
        if by:
            args += ["--by", by]
        return (await self._call(*args))["claim"]

    async def remove_claim(self, name: str) -> dict[str, Any]:
        return (await self._call("claim", "remove", name))["released"]
