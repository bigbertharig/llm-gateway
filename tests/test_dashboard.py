"""The dashboard (docs/DASHBOARD.md): page, rig API and overrides, model/benchmark summaries."""

from __future__ import annotations

import json

from llm_gateway.dashboard import sources
from test_manager import menv  # noqa: F401  (fixture: the gateway with the manager and a fake rig)


def test_page_and_rig_status(menv):  # noqa: F811
    c = menv["client"]
    page = c.get("/dashboard/")
    assert page.status_code == 200 and "Rig dashboard" in page.text and "/dashboard/api/" in page.text
    assert 'id="dashboard-refresh"' in page.text
    assert '$("#dashboard-refresh").onclick = () => refresh(true)' in page.text
    assert '" · last updated " + localClock(last[tab])' in page.text
    assert "if (refreshInFlight) return refreshInFlight" in page.text
    assert page.text.count('id="chat-context-meter"') == 1
    assert "Updating through the current tool/model step." in page.text
    assert 'api("chat/options")' in page.text
    assert "new EventSource" in page.text
    assert 'assistant · progress' in page.text
    assert 'id="chat-toggle-tools"' in page.text and "desktop-show-tools" in page.text
    assert "chatToolDisclosure" in page.text and 'document.createElement("details")' in page.text
    assert "chat-tool-disclosure" in page.text and "Show earlier activity (" in page.text
    assert "chatVisibleToolRows += 80" in page.text
    assert "deliverCodexAtNextBoundary" in page.text and "error.status = res.status" in page.text
    assert 'error.status !== 409' in page.text and 'steer = !steer' in page.text
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


def test_retired_queue_routes_are_absent(menv):  # noqa: F811
    client = menv["client"]
    page = client.get("/dashboard/").text
    assert 'data-tab="orchestration"' not in page
    assert 'api("orchestration' not in page
    assert client.get("/dashboard/api/rig").status_code == 200
    assert client.get("/dashboard/api/orchestration").status_code == 404
    assert client.post("/dashboard/api/orchestration/kill_plan", json={}).status_code == 404
