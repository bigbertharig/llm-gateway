"""The dashboard (docs/DASHBOARD.md): page, rig API and overrides, model/benchmark summaries, orchestration."""

from __future__ import annotations

import json

from llm_gateway.dashboard import sources
from test_manager import menv  # noqa: F401  (fixture: the gateway with the manager and a fake rig)


def test_page_and_rig_status(menv):  # noqa: F811
    c = menv["client"]
    page = c.get("/dashboard/")
    assert page.status_code == 200 and "Rig dashboard" in page.text and "/dashboard/api/" in page.text
    assert page.text.count('id="chat-context-meter"') == 1
    assert "Updating through the current tool/model step." in page.text
    assert 'api("chat/options")' in page.text
    assert "new EventSource" in page.text
    assert 'assistant · progress' in page.text
    assert 'aria-valuetext", "Context usage pending"' in page.text
    assert 'windowDurationMins === 300' in page.text
    assert 'remaining <= 10 ? "bad" : remaining <= 25 ? "warn"' in page.text
    assert 'id="chat-limit-alert"' in page.text
    assert "renderChatMarkdown" in page.text and "Attach images" in page.text and "Enter sends" in page.text
    # Transcript/capacity polling must not replace an in-progress composer draft.
    assert "const refreshingDisplayedChat = sameChat && displayedChatId === id" in page.text
    assert "clearChatView({preserveComposer})" in page.text
    assert c.get("/dashboard", follow_redirects=False).status_code in (301, 302, 307)
    rig = c.get("/dashboard/api/rig").json()
    assert [g["gpu"] for g in rig["gpus"]] == [0, 1, 2]
    assert "temp_c" in rig["gpus"][0]["observed"]


def test_overrides_go_through_the_manager(menv):  # noqa: F811
    c = menv["client"]
    lid = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": "lanes", "run_id": "r",
                                             "gpu_ids": [2]}).json()["lease"]["lease_id"]
    assert c.post("/v1/manager/claims", json={"name": "pair", "gpu_ids": [1, 2]}).status_code == 201
    r = c.post("/dashboard/api/rig/kill", json={"gpu_ids": "2", "reason": "test"})
    assert r.status_code == 200 and r.json()["released"] == [lid] and r.json()["claims_dropped"] == ["pair"]
    assert c.post("/v1/manager/claims", json={"name": "solo", "gpu_ids": [1]}).status_code == 201
    assert c.post("/dashboard/api/rig/unclaim", json={"name": "solo"}).status_code == 200
    assert menv["rig"]()["claims"] == []
    assert c.post("/dashboard/api/rig/kill", json={"gpu_ids": []}).status_code == 400


def test_models_summary_is_read_from_the_catalog_not_benchmarking(tmp_path, monkeypatch):
    path = tmp_path / "model_catalog.json"
    path.write_text(json.dumps({"models": {}, "summary": {"models": [{"model": "m:9b"}], "roles": {}, "key_tests": []}}))
    monkeypatch.setenv("MODEL_CATALOG", str(path))
    assert sources.models_summary()["models"] == [{"model": "m:9b"}]
    monkeypatch.setenv("MODEL_CATALOG", str(tmp_path / "missing.json"))
    assert "error" in sources.models_summary()


def test_benchmark_evidence_uses_imported_gateway_catalog(tmp_path, monkeypatch, menv):  # noqa: F811
    path = tmp_path / "model_catalog.json"
    path.write_text(json.dumps({"models": {}, "summary": {"models": [
        {"model": "m:9b", "status": "active", "scores": {"code": {"score": 0.8}},
         "runtime": {"decode_tps": 20}, "profiles": ["m"]}], "key_tests": ["code"]}}))
    monkeypatch.setenv("MODEL_CATALOG", str(path))
    out = menv["client"].get("/dashboard/api/benchmarks").json()
    assert out["source"] == "gateway_model_catalog" and out["key_tests"] == ["code"]
    assert out["models"][0]["scores"]["code"]["score"] == 0.8


def test_orchestration_tab_uses_the_orchestration_api(menv, monkeypatch):  # noqa: F811
    def fake_request(method, path, body=None):
        if path.endswith("/status"):
            return {"active_batch_count": 1, "workers": [], "brain": {"heartbeat_status": "ok"}}
        if path.endswith("/diagnostics"):
            return {"ok": True, "checks": []}
        if "/activity?" in path:
            return {"batch_id": "b1", "events": [{"event": "task_started"}], "has_more": False}
        if path.endswith("/kill_plan"):
            return {"ok": True, "message": f"Killed batch {body['batch_id']}"}
        if path.endswith("/requeue_worker"):
            return {"ok": True, "message": f"requeued {body['worker']}"}
        raise AssertionError((method, path, body))

    monkeypatch.setattr(sources, "orchestration_request", fake_request)
    c = menv["client"]
    assert c.get("/dashboard/api/orchestration").json()["active_batch_count"] == 1
    assert c.get("/dashboard/api/orchestration/diagnostics").json()["ok"] is True
    assert c.get("/dashboard/api/orchestration/activity?batch_id=b1").json()["events"][0]["event"] == "task_started"
    assert c.get("/dashboard/api/orchestration/options").status_code == 405
    assert c.post("/dashboard/api/orchestration/kill_plan", json={"batch_id": "b1"}).json()["message"] == "Killed batch b1"
    assert c.post("/dashboard/api/orchestration/requeue_worker", json={"worker": "gpu-3"}).json()["ok"]
    assert c.post("/dashboard/api/orchestration/rm_rf", json={}).status_code == 404
    assert c.post("/dashboard/api/orchestration/start_plan", json={}).status_code == 404


def test_a_missing_orchestration_socket_is_reported_not_raised(menv, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ORCHESTRATION_API_SOCKET", str(tmp_path / "missing.sock"))
    r = menv["client"].get("/dashboard/api/orchestration")
    assert r.status_code == 503 and any(
        name in r.json()["error"]["message"] for name in ("FileNotFoundError", "AttributeError"))
