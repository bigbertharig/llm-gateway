"""Gateway prepares plans and sends only resolved work to orchestration."""

import json

from llm_gateway.dashboard import plan_requests


def test_prepare_plan_merges_defaults_and_submits_one_resolved_request(tmp_path, monkeypatch):
    plan = tmp_path / "plans" / "shoulders" / "sample"
    (plan / "scripts").mkdir(parents=True)
    (plan / "plan.md").write_text("# Sample\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(plan_requests, "default_plan_config", lambda *_: {"RUN_MODE": "fresh", "PRIORITY": "normal"})
    monkeypatch.setattr(plan_requests, "orchestration_action", lambda action, body: calls.append((action, body)) or {"ok": True})

    out = plan_requests.PlanRequests(tmp_path, {}).start_plan("sample", json.dumps({"PRIORITY": "urgent"}))

    assert out["ok"]
    assert calls == [("submit_prepared", {"plan_name": "sample", "plan_scope": "shoulders",
                                           "starter_file": "", "config": {"RUN_MODE": "fresh", "PRIORITY": "urgent"}})]


def test_invalid_plan_does_not_reach_executor(tmp_path, monkeypatch):
    monkeypatch.setattr(plan_requests, "orchestration_action", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert not plan_requests.PlanRequests(tmp_path, {}).start_plan("missing")["ok"]


def test_gateway_assignments_are_passed_as_prepared_config(tmp_path, monkeypatch):
    plan = tmp_path / "plans" / "shoulders" / "sample"
    (plan / "scripts").mkdir(parents=True)
    (plan / "plan.md").write_text("# Sample\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(plan_requests, "default_plan_config", lambda *_: {})
    monkeypatch.setattr(plan_requests, "orchestration_action", lambda action, body: calls.append(body) or {"ok": True})
    assignments = {"schema": 1, "assignments": {"extract": {"model": "m", "role": "text_extraction"}}}
    out = plan_requests.PlanRequests(tmp_path, {}).start_plan(
        "sample", json.dumps({"GATEWAY_MODEL_ASSIGNMENTS": "spoofed"}),
        gateway_model_assignments=assignments)
    assert out["ok"]
    assert json.loads(calls[0]["config"]["GATEWAY_MODEL_ASSIGNMENTS"]) == assignments
