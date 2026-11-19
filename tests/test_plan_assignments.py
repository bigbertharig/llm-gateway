"""Plan role assignment uses reviewed catalog roles and the live Manager fit."""

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway.dashboard import plan_assignments, plan_requests, routes, sources
from llm_gateway.manager.errors import ManagerError


def test_reads_only_explicit_worker_llm_roles(tmp_path):
    plan = tmp_path / "plans" / "shoulders" / "sample"
    (plan / "scripts").mkdir(parents=True)
    (plan / "plan.md").write_text(
        "# Sample\n### extract\n- **executor**: worker\n- **task_class**: llm\n"
        "- **llm_role**: text_extraction\n- **llm_fallback_roles**: single_1060\n"
        "- **command**: `run`\n### summarize\n- **executor**: brain\n"
        "- **task_class**: llm\n- **llm_role**: brain\n- **command**: `run`\n",
        encoding="utf-8",
    )
    assert plan_assignments.plan_role_requests(tmp_path, "sample", "shoulders", "") == [
        {"task_id": "extract", "roles": ["text_extraction", "single_1060"]}
    ]


def test_falls_back_only_after_real_no_fit(monkeypatch):
    monkeypatch.setattr(plan_assignments.models, "catalog", lambda: {
        "roles": {"quality": {"model": "large", "profile": "default"},
                  "compact": {"model": "small", "profile": "default"}},
        "models": {"large": {"status": "active"}, "small": {"status": "active"}},
    })

    class Manager:
        async def fit_model(self, model, **kwargs):
            if model == "large":
                raise ManagerError(422, "no capacity on this rig")
            return {"fits": True}

    doc, decisions = asyncio.run(plan_assignments.assign_plan_models(
        Manager(), [{"task_id": "step", "roles": ["quality", "compact"]}]))
    assert doc == {"schema": 1, "assignments": {"step": {"model": "small", "role": "compact"}}}
    assert decisions[0]["state"] == "fits_now"


def test_busy_preferred_model_uses_declared_available_fallback(monkeypatch):
    monkeypatch.setattr(plan_assignments.models, "catalog", lambda: {
        "roles": {"quality": {"model": "large", "profile": "default"},
                  "compact": {"model": "small", "profile": "default"}},
        "models": {"large": {"status": "active"}, "small": {"status": "active"}},
    })

    class Manager:
        async def fit_model(self, model, **kwargs):
            if model == "large":
                raise ManagerError(503, "currently busy")
            return {"fits": True}

    doc, decisions = asyncio.run(plan_assignments.assign_plan_models(
        Manager(), [{"task_id": "step", "roles": ["quality", "compact"]}]))
    assert doc["assignments"]["step"]["model"] == "small"
    assert decisions[0]["state"] == "fits_now"


def test_busy_model_without_fallback_is_queued(monkeypatch):
    monkeypatch.setattr(plan_assignments.models, "catalog", lambda: {
        "roles": {"quality": {"model": "large", "profile": "default"}},
        "models": {"large": {"status": "active"}},
    })

    class Manager:
        async def fit_model(self, model, **kwargs):
            raise ManagerError(503, "currently busy")

    doc, decisions = asyncio.run(plan_assignments.assign_plan_models(
        Manager(), [{"task_id": "step", "roles": ["quality"]}]))
    assert doc["assignments"]["step"]["model"] == "large"
    assert decisions[0]["state"] == "wait_for_capacity"


def test_dashboard_passes_manager_assignment_to_orchestration(tmp_path, monkeypatch):
    plan = tmp_path / "plans" / "shoulders" / "sample"
    (plan / "scripts").mkdir(parents=True)
    (plan / "plan.md").write_text(
        "# Sample\n### extract\n- **executor**: worker\n- **task_class**: llm\n"
        "- **llm_role**: text_extraction\n- **command**: `run`\n", encoding="utf-8")
    monkeypatch.setattr(sources, "shared_root", lambda: tmp_path)
    monkeypatch.setattr(plan_requests, "default_plan_config", lambda *_: {})
    monkeypatch.setattr(plan_assignments.models, "catalog", lambda: {
        "roles": {"text_extraction": {"model": "small", "profile": "default"}},
        "models": {"small": {"status": "active"}},
    })
    submitted = []
    monkeypatch.setattr(plan_requests, "orchestration_action",
                        lambda action, body: submitted.append((action, body)) or {"ok": True})

    class Manager:
        async def fit_model(self, model, **kwargs):
            assert model == "small" and kwargs["use"] == "worker"
            return {"fits": True}

    app = FastAPI()
    app.include_router(routes.router(Manager()))
    response = TestClient(app).post("/dashboard/api/orchestration/start_plan", json={"plan_name": "sample"})
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["model_assignments"][0]["model"] == "small"
    assert submitted[0][0] == "submit_prepared"
    prepared = json.loads(submitted[0][1]["config"]["GATEWAY_MODEL_ASSIGNMENTS"])
    assert prepared == {"schema": 1, "assignments": {"extract": {"model": "small", "role": "text_extraction"}}}
