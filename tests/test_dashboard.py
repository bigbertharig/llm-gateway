"""The dashboard (docs/DASHBOARD.md): page, rig API and overrides, model/benchmark summaries, orchestration."""

from __future__ import annotations

import json
import os
import time

from llm_gateway.dashboard import sources
from test_manager import menv  # noqa: F401  (fixture: the gateway with the manager and a fake rig)


def test_page_and_rig_status(menv):  # noqa: F811
    c = menv["client"]
    page = c.get("/dashboard/")
    assert page.status_code == 200 and "Rig dashboard" in page.text and "/dashboard/api/" in page.text
    assert "renderChatMarkdown" in page.text and "Attach images" in page.text and "Enter sends" in page.text
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


def test_benchmarks_summary_finds_lanes_and_campaigns(tmp_path):
    logs = tmp_path / "logs" / "benchmarks"
    run = logs / "worker_lanes_20261020_120000"
    run.mkdir(parents=True)
    (run / "queue.txt").write_text("phi-4:14b\n")
    (run / "lane_gpu1-2.log").write_text("12:00:00 [gpu1-2] Phase 4 smoke\n")
    (run / "lane_gpu4-5.log").write_text("12:00:00 [gpu4-5] LANE_DONE\n")
    camp = logs / "campaigns" / "history" / "c1" / "20261020_1"
    camp.mkdir(parents=True)
    (camp / "status.json").write_text(json.dumps({"campaign": "c1", "run_id": "20261020_1", "status": "running",
                                                  "blocks": {"a": {"status": "completed"}, "b": {"status": "running_suite"}}}))
    out = sources.benchmarks_summary(tmp_path)  # no claims known: log age decides
    lanes = {l["lane"]: l for l in out["lanes"]}
    assert lanes["gpu 1-2"]["active"] and not lanes["gpu 4-5"]["active"] and lanes["gpu 1-2"]["queued"] == ["phi-4:14b"]
    (c,) = out["campaigns"]
    assert c["blocks"] == {"completed": 1, "running_suite": 1} and c["current"] == ["b (running_suite)"]
    # With the rig's claims: a lane whose log is old still runs while it holds its claim; an older run of
    # the same card group is never the running one.
    old = os.path.getmtime(run / "lane_gpu1-2.log") - 7200
    os.utime(run / "lane_gpu1-2.log", (old, old))
    earlier = logs / "worker_lanes_20261019_120000"
    earlier.mkdir()
    (earlier / "lane_gpu1-2.log").write_text("11:00:00 [gpu1-2] Phase 2\n")
    os.utime(earlier, (old - 60, old - 60))
    out = sources.benchmarks_summary(tmp_path, claims={"lane-1-2"})
    rows = [(l["run"], l["lane"], l["active"]) for l in out["lanes"] if l["lane"] == "gpu 1-2"]
    assert ("worker_lanes_20261020_120000", "gpu 1-2", True) in rows
    assert ("worker_lanes_20261019_120000", "gpu 1-2", False) in rows


def test_benchmarks_summary_keeps_active_jobs_and_only_one_day_of_finished_history(tmp_path):
    logs = tmp_path / "logs" / "benchmarks"
    stale = time.time() - sources.RECENT_HISTORY_S - 60
    for name, status in (("finished", "completed"), ("stale_running", "running"),
                         ("long_running", "running")):
        path = logs / "campaigns" / "history" / name / "run" / "status.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"campaign": name, "status": status, "blocks": {}}))
        if name != "long_running":
            os.utime(path, (stale, stale))
    for group, done in (("1", True), ("2", False)):
        run = logs / f"worker_lanes_{group}"
        run.mkdir()
        lane = run / f"lane_gpu{group}.log"
        lane.write_text("LANE_DONE\n" if done else "still running\n")
        os.utime(lane, (stale, stale))
    out = sources.benchmarks_summary(tmp_path, claims={"lane-2"})
    assert [c["campaign"] for c in out["campaigns"]] == ["long_running"]
    assert [(l["lane"], l["active"]) for l in out["lanes"]] == [("gpu 2", True)]


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
    monkeypatch.setattr(sources, "orchestration_options", lambda: {"ok": True, "plans": ["p"]})
    c = menv["client"]
    assert c.get("/dashboard/api/orchestration").json()["active_batch_count"] == 1
    assert c.get("/dashboard/api/orchestration/diagnostics").json()["ok"] is True
    assert c.get("/dashboard/api/orchestration/activity?batch_id=b1").json()["events"][0]["event"] == "task_started"
    assert c.get("/dashboard/api/orchestration/options").json()["plans"] == ["p"]
    assert c.post("/dashboard/api/orchestration/kill_plan", json={"batch_id": "b1"}).json()["message"] == "Killed batch b1"
    assert c.post("/dashboard/api/orchestration/requeue_worker", json={"worker": "gpu-3"}).json()["ok"]
    assert c.post("/dashboard/api/orchestration/rm_rf", json={}).status_code == 404


def test_a_missing_orchestration_socket_is_reported_not_raised(menv, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ORCHESTRATION_API_SOCKET", str(tmp_path / "missing.sock"))
    r = menv["client"].get("/dashboard/api/orchestration")
    assert r.status_code == 503 and any(
        name in r.json()["error"]["message"] for name in ("FileNotFoundError", "AttributeError"))
