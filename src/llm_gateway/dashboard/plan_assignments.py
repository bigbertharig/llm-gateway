"""Resolve explicit plan task roles through the gateway manager before submission.

The plan remains a workflow. Only tasks that opt into ``llm_role`` are assigned
here; older plans keep their current model fields and behavior.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..manager import models
from ..manager.errors import ManagerError
from .plan_catalog import discover_plan_starters, discover_plans, plan_dir


FIELD = re.compile(r"^- \*\*(executor|task_class|llm_role|llm_fallback_roles)\*\*:\s*(.*?)\s*$")
ROLE = re.compile(r"^[a-z][a-z0-9_]*$")


def plan_role_requests(shared: Path, plan_name: str, scope: str, starter_file: str) -> list[dict[str, Any]]:
    """Read only a validated starter; return worker LLM tasks with an explicit role."""
    if scope not in {"shoulders", "arms"} or plan_name not in discover_plans(shared, scope):
        return []
    starter = starter_file or "plan.md"
    if starter not in discover_plan_starters(shared, plan_name, scope):
        return []
    source = (plan_dir(shared, plan_name, scope) / starter).read_text(encoding="utf-8")
    requests = []
    for section in re.split(r"\n### ", "\n" + source)[1:]:
        lines = section.splitlines()
        task_id = lines[0].strip() if lines else ""
        fields = {m.group(1): m.group(2).strip() for line in lines[1:] if (m := FIELD.fullmatch(line.strip()))}
        if fields.get("executor", "worker") != "worker" or fields.get("task_class") != "llm":
            continue
        role = fields.get("llm_role", "")
        if not role:
            continue
        fallbacks = [r.strip() for r in fields.get("llm_fallback_roles", "").split(",") if r.strip()]
        if not task_id or not all(ROLE.fullmatch(r) for r in [role, *fallbacks]):
            raise ValueError(f"{task_id or 'plan'}: invalid LLM role")
        requests.append({"task_id": task_id, "roles": list(dict.fromkeys([role, *fallbacks]))})
    return requests


async def assign_plan_models(manager: Any, requests: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Use the reviewed role order and live Manager fit, retaining busy first choices."""
    catalog = models.catalog()
    roles = catalog.get("roles") or {}
    entries = catalog.get("models") or {}
    assignments: dict[str, dict[str, str]] = {}
    decisions = []
    for request in requests:
        task_id = request["task_id"]
        failures = []
        waiting: tuple[str, str] | None = None
        for role_name in request["roles"]:
            role = roles.get(role_name)
            if not isinstance(role, dict):
                failures.append(f"{role_name}: unknown benchmark role")
                continue
            model = role.get("model")
            entry = entries.get(model) or {}
            if entry.get("status") != "active" or entry.get("in_testing"):
                failures.append(f"{role_name}: no active model")
                continue
            try:
                await manager.fit_model(model, use="worker", profile=role.get("profile") or "default",
                                        requester=f"plan:{task_id}")
                state = "fits_now"
            except ManagerError as exc:
                if exc.status == 503:
                    if waiting is None:
                        waiting = (model, role_name)
                    continue
                elif exc.status in (404, 422):
                    failures.append(f"{role_name}: {exc}")
                    continue
                else:
                    raise
            assignments[task_id] = {"model": model, "role": role_name}
            decisions.append({"task_id": task_id, "model": model, "role": role_name, "state": state})
            break
        else:
            if waiting is None:
                raise ValueError(f"{task_id}: no benchmarked model fits this rig ({'; '.join(failures)})")
            model, role_name = waiting
            assignments[task_id] = {"model": model, "role": role_name}
            decisions.append({"task_id": task_id, "model": model, "role": role_name,
                              "state": "wait_for_capacity"})
    return {"schema": 1, "assignments": assignments}, decisions
