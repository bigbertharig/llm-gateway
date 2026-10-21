"""The dashboard (docs/DASHBOARD.md): page, rig API and overrides, model/benchmark summaries, orchestration."""

from __future__ import annotations

import json
import os
import textwrap
import time

from llm_gateway.dashboard import sources
from test_manager import menv  # noqa: F401  (fixture: the gateway with the manager and a fake rig)


def test_page_and_rig_status(menv):  # noqa: F811
    c = menv["client"]
    page = c.get("/dashboard/")
    assert page.status_code == 200 and "Rig dashboard" in page.text and "/dashboard/api/" in page.text
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


def _bench(tmp_path):
    root = tmp_path / "bench"
    (root / "results" / "runtime_experiments").mkdir(parents=True)
    (root / "model_registry.json").write_text(json.dumps({"models": {
        "m:9b": {"status": "active", "source_url": "https://huggingface.co/x/y/resolve/main/m.gguf",
                 "schedule": {"placement": "single_gpu", "tier": 1}, "profiles": {"default": {}, "thinking": {}}},
        "old:7b": {"status": "archived", "profiles": {"default": {}}}}}))
    (root / "model_roles.json").write_text(json.dumps({"roles": {"single_1060": {"model": "m:9b"}}}))
    v4 = {"config_id": "lm-eval-0.4.11-reasoning-extract-v4"}
    v5 = {"config_id": "lm-eval-0.4.11-reasoning-extract-v5"}
    recs = [{"model": "m:9b", "test_id": "bbh_flexible", "score": 0.80, "run_class": "smoke", "run_at": "2026-10-19T01:00:00Z"},
            {"model": "m:9b", "test_id": "bbh_flexible", "score": 0.85, "run_class": "validated", "run_at": "2026-10-18T01:00:00Z"},
            # same run re-scored by the newer extractor: earlier run_at, but it wins
            {"model": "m:9b", "test_id": "drop_flexible", "score": 0.2, "run_class": "smoke", "run_at": "2026-10-19T01:00:09Z", "runtime": v4},
            {"model": "m:9b", "test_id": "drop_flexible", "score": 0.6, "run_class": "smoke", "run_at": "2026-10-19T01:00:00Z",
             "runtime": v5, "extractor_failure_count": 2, "sample_count": 10},
            {"model": "m:9b", "test_id": "custom_a", "score": 1.0, "run_class": "smoke", "run_at": "2026-10-19T01:00:00Z"},
            {"model": "m:9b", "test_id": "custom_b", "score": 0.5, "run_class": "smoke", "run_at": "2026-10-19T01:00:00Z"}]
    (root / "results" / "model_benchmark_records.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    (root / "results" / "runtime_experiments" / "m_9b_runtime_characterization_20261020.json").write_text(json.dumps(
        {"runtime": {"ctx_size": 26880}, "decode": {"generation_tokens_per_second": 16.6}}))
    return root


def test_models_summary_prefers_limit50_runs_and_reads_roles_and_speed(tmp_path):
    out = sources.models_summary(_bench(tmp_path))
    first = out["models"][0]
    assert first["model"] == "m:9b" and first["roles"] == ["single_1060"] and first["profiles"] == ["default", "thinking"]
    assert first["scores"]["bbh"]["score"] == 0.85 and first["scores"]["bbh"]["run_class"] == "validated"
    assert first["scores"]["bbh"]["unreadable"] is None
    assert first["scores"]["drop"]["score"] == 0.6 and first["scores"]["drop"]["unreadable"] == 2
    assert first["pipeline"] == 0.75 and first["runtime"]["decode_tps"] == 16.6
    assert out["models"][-1]["model"] == "old:7b"


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


FAKE_ORCH = textwrap.dedent("""\
    class Controls:
        def __init__(self, shared_path, config):
            self.shared_path, self.config = shared_path, config
        def options(self):
            return {"ok": True, "plans": ["p"], "plan_scopes": {"p": "shoulders"}}
        def kill_plan(self, batch_id):
            return {"ok": True, "message": f"Killed batch {batch_id}"}
        def requeue_worker(self, worker=""):
            return {"ok": True, "message": f"requeued 0 task(s) of {worker}"}
    def summarize(shared_path, config):
        return {"counts": {"queue": 1}, "workers": [], "alerts": [], "config_seen": config.get("marker")}
""")


def test_orchestration_tab_uses_the_orchestration_package(menv, tmp_path, monkeypatch):  # noqa: F811
    pkg = tmp_path / "orch" / "scripts" / "dashboard"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(FAKE_ORCH)
    shared = tmp_path / "shared"
    (shared / "agents").mkdir(parents=True)
    (shared / "agents" / "config.json").write_text(json.dumps({"marker": "cfg"}))
    monkeypatch.setenv("ORCHESTRATION_ROOT", str(tmp_path / "orch"))
    monkeypatch.setenv("SHARED_ROOT", str(shared))
    c = menv["client"]
    assert c.get("/dashboard/api/orchestration").json()["config_seen"] == "cfg"
    assert c.get("/dashboard/api/orchestration/options").json()["plans"] == ["p"]
    assert c.post("/dashboard/api/orchestration/kill_plan", json={"batch_id": "b1"}).json()["message"] == "Killed batch b1"
    assert c.post("/dashboard/api/orchestration/requeue_worker", json={"worker": "gpu-3"}).json()["ok"]
    assert c.post("/dashboard/api/orchestration/rm_rf", json={}).status_code == 404


def test_a_missing_orchestration_checkout_is_reported_not_raised(menv, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ORCHESTRATION_ROOT", str(tmp_path / "nowhere"))
    monkeypatch.setenv("SHARED_ROOT", str(tmp_path))
    r = menv["client"].get("/dashboard/api/orchestration")
    assert r.status_code == 503 and "ORCHESTRATION_ROOT" in r.json()["error"]["message"]
