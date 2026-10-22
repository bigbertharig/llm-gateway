"""Model fit (manager/fit.py) on synthetic GGUF headers, and `models/load` end to end against the fake rig."""

from __future__ import annotations

import json
import struct
import textwrap

import pytest

from llm_gateway.manager import fit, models
from test_manager import menv  # noqa: F401  (fixture: the gateway with the manager and a fake rig)

MIB = 1 << 20
Q8_0 = 8  # ggml type id: 32 values in 34 bytes


def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_gguf(path, meta: dict, tensors: dict[str, int]) -> str:
    """A GGUF header with u32/str/bool-array metadata and q8_0 tensors of the given element counts."""
    out = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(meta))
    for key, value in meta.items():
        out += _s(key)
        if isinstance(value, str):
            out += struct.pack("<I", 8) + _s(value)
        elif isinstance(value, list):
            out += struct.pack("<IIQ", 9, 7, len(value)) + bytes(int(v) for v in value)
        else:
            out += struct.pack("<II", 4, int(value))
    for name, n in tensors.items():
        out += _s(name) + struct.pack("<IQ", 1, n) + struct.pack("<IQ", Q8_0, 0)
    path.write_bytes(out)
    return str(path)


def dense(tmp_path, n_layer=10, layer_mb=100, output_mb=50, embd_mb=40, ctx_train=32768, name="m.gguf", **extra):
    per = layer_mb * MIB // 34 * 32  # q8_0 elements for layer_mb MiB
    tensors = {f"blk.{i}.attn_q.weight": per // 2 for i in range(n_layer)}
    tensors |= {f"blk.{i}.ffn_up.weight": per - per // 2 for i in range(n_layer)}
    tensors |= {"output.weight": output_mb * MIB // 34 * 32, "token_embd.weight": embd_mb * MIB // 34 * 32}
    meta = {"general.architecture": "llama", "llama.block_count": n_layer, "llama.context_length": ctx_train,
            "llama.attention.head_count": 8, "llama.attention.head_count_kv": 2,
            "llama.attention.key_length": 128, "llama.attention.value_length": 128, **extra}
    return write_gguf(tmp_path / name, meta, tensors)


def card(i, mb=6144):
    return {"index": i, "total_mb": mb, "used_mb": 0}


def test_shape_reads_weights_kv_and_puts_the_embedding_on_the_host(tmp_path):
    s = fit.shape_for(dense(tmp_path), "m")
    assert s.n_layers == 10 and s.n_ctx_train == 32768
    assert round(s.weights_mb) == 10 * 100 + 50  # layers + output; token_embd stays in host RAM
    assert round(s.host_bytes / MIB) == 40
    assert s.kv_per_token[0] == 2 * (128 + 128) * 34 / 32  # KV heads x (key + value) x q8_0


def test_hybrid_and_next_token_layers(tmp_path):
    path = dense(tmp_path, n_layer=9, **{"llama.full_attention_interval": 4, "llama.nextn_predict_layers": 1})
    s = fit.shape_for(path, "h")
    assert [i for i, kv in enumerate(s.kv_per_token) if kv] == [3, 7]  # every 4th; layer 8 is skipped
    assert s.weights[8] == 0 and round(s.weights_mb) == 8 * 100 + 50


def test_sliding_window_layers_cap_their_kv(tmp_path):
    path = dense(tmp_path, n_layer=4, **{"llama.attention.sliding_window": 1024,
                                         "llama.attention.sliding_window_pattern": [1, 1, 1, 0]})
    s = fit.shape_for(path, "w")
    assert s.kv_window == [1024, 1024, 1024, None]


def test_split_puts_the_output_on_the_last_card_and_balances(tmp_path):
    s = fit.shape_for(dense(tmp_path, n_layer=20, layer_mb=200, output_mb=400), "m")
    p = fit.plan(s, [card(3), card(1)], ctx_min=4096)
    assert p["fits"] and p["gpus"] == [1, 3]                     # CUDA order: lowest GPU first
    split = [int(x) for x in p["tensor_split"].split(",")]
    assert sum(split) == 20 + 1                                  # every layer plus the output slot
    a, b = p["cards"]
    assert a["layers"] + b["layers"] == 20 and abs(a["headroom_mb"] - b["headroom_mb"]) < 300
    assert min(a["headroom_mb"], b["headroom_mb"]) >= 0
    assert p["ctx"] % fit.CTX_STEP == 0 and p["ctx"] <= 32768


def test_capped_at_the_trained_context_or_the_request(tmp_path):
    s = fit.shape_for(dense(tmp_path, n_layer=4, layer_mb=50, ctx_train=8192), "small")
    assert fit.plan(s, [card(1)])["ctx"] == 8192
    assert fit.plan(s, [card(1)])["ctx_limit"] == "trained context"
    assert fit.plan(s, [card(1)], ctx=6000)["ctx"] == 5888       # exact request, rounded to the step


def test_wont_fit_says_why_and_suggests_cards(tmp_path):
    s = fit.shape_for(dense(tmp_path, n_layer=40, layer_mb=400), "big")  # 16 GB of weights
    p = fit.plan(s, [card(1), card(2)])
    assert not p["fits"] and p["reason"].startswith("won't fit: big needs about")
    alts = fit.suggest(s, [card(1), card(2), card(3), card(4), card(0, 24564)])
    assert any("GPU 0" in a for a in alts) and any(a.startswith("4 card(s)") for a in alts)


def test_harness_picks_one_card_for_small_context_and_splits_for_big(tmp_path):
    s = fit.shape_for(dense(tmp_path, n_layer=20, layer_mb=200, output_mb=400, ctx_train=262144), "m")
    free = [card(1), card(2), card(3), card(0, 24564)]
    one = fit.choose(s, free)                      # no context asked: one small card is enough
    assert one["gpus"] == [1]
    two = fit.choose(s, free, ctx=one["ctx"] * 2)  # more than one 1060 holds: split on two 1060s
    assert two["gpus"] == [1, 2]
    huge = fit.choose(s, free, ctx=one["ctx"] * 6)  # more than two 1060s hold: the big card, not three small
    assert huge["gpus"] == [0]


def test_harness_prefers_the_big_card_over_many_small_ones(tmp_path):
    s = fit.shape_for(dense(tmp_path, n_layer=40, layer_mb=400), "big")  # 16 GB: needs 3+ 1060s
    p = fit.choose(s, [card(1), card(2), card(3), card(4), card(0, 24564)])
    assert p["gpus"] == [0]


# ------------------------------------------------------------------ end to end (fake rig)

FAKE_LIBRARY = textwrap.dedent("""\
    import copy, json
    def load_registry(path): return json.load(open(path))
    def find_model(registry, mid):
        if mid not in registry["models"]: raise KeyError(f"unknown model {mid}")
        return mid, registry["models"][mid]
    def runtime_image(registry): return "llama-runtime:test"
    def resolve(mid, profile="default", registry=None):
        e = registry["models"][mid]; merged = copy.deepcopy(e["profiles"]["default"])
        merged.setdefault("extra_args", [])
        return {"model": mid, "profile": profile, "status": e["status"], "gguf_path": e["gguf"], **merged}
""")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    root = tmp_path / "bench"
    (root / "scripts" / "active").mkdir(parents=True)
    (root / "scripts" / "active" / "model_library.py").write_text(FAKE_LIBRARY)
    small = dense(tmp_path, n_layer=20, layer_mb=200, output_mb=400, name="small.gguf")
    big = dense(tmp_path, n_layer=40, layer_mb=400, name="big.gguf")
    prof = {"runtime": {"batch_size": 64, "parallel": 1}, "extra_args": ["--reasoning", "off"]}
    mid = dense(tmp_path, n_layer=20, layer_mb=400, output_mb=400, name="mid.gguf")   # needs two 1060s
    (root / "model_registry.json").write_text(json.dumps({"models": {
        "mid:14b": {"status": "candidate", "gguf": mid, "profiles": {"default": prof}},
        "small:9b": {"status": "candidate", "gguf": small, "profiles": {"default": prof}},
        "big:30b": {"status": "active", "gguf": big, "profiles": {"default": prof}}}}))
    monkeypatch.setenv("BENCHMARKING_ROOT", str(root))
    monkeypatch.setattr(models, "_library", None)


def test_fit_endpoint_answers_without_loading(menv, registry):  # noqa: F811
    c = menv["client"]
    r = c.post("/v1/manager/models/fit", json={"model": "small:9b", "cards": 2})
    assert r.status_code == 200 and r.json()["fits"] and r.json()["gpus"] == [1, 2]
    r = c.post("/v1/manager/models/fit", json={"model": "big:30b", "gpu_ids": [1, 2]})
    assert r.status_code == 422 and "won't fit" in r.json()["error"]["message"]
    assert "GPU [0]" in r.json()["error"]["message"] or "GPU 0" in r.json()["error"]["message"]
    assert menv["leases"]() == [] and not menv["rig"]().get("plan_items")
    r = c.post("/v1/manager/models/fit", json={"model": "big:30b"})  # no cards: the harness chooses
    assert r.status_code == 200 and r.json()["gpus"] == [0] and r.json()["chosen_by"].startswith("harness")


def test_load_model_leases_queues_the_spec_and_reports_ready(menv, registry):  # noqa: F811
    c = menv["client"]
    r = c.post("/v1/manager/models/load", json={"model": "small:9b", "gpu_ids": [1, 2],
                                                "extra_args": ["--reasoning", "on"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["profile"] == "small-9b-g1-2" and out["port"] == 11435 and out["state"] == "ready"
    (lease,) = menv["leases"]()
    assert lease["controller"] == "benchmarks" and lease["gpu_ids"] == [1, 2]
    item = menv["rig"]()["plan_items"][-1]
    spec = item["args"]["spec"]
    assert spec["ctx_size"] == out["ctx"] and spec["tensor_split"] == out["tensor_split"]
    assert spec["n_gpu_layers"] == "all" and spec["extra_args"] == ["--reasoning", "off", "--reasoning", "on"]
    assert item["args"]["cache_ram"] == 0  # benchmark use: no host prompt cache
    assert c.post("/v1/manager/profiles/small-9b-g1-2/unload").status_code == 200
    assert menv["leases"]() == []


def test_load_model_that_wont_fit_never_reaches_the_rig(menv, registry):  # noqa: F811
    r = menv["client"].post("/v1/manager/models/load", json={"model": "big:30b", "cards": 2})
    assert r.status_code == 422
    assert menv["leases"]() == [] and not menv["rig"]().get("plan_items")


# ------------------------------------------------------------------ reservations and overrides (MANAGER.md)

def _hold(c, gpu, owner="lanes"):
    r = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": owner, "run_id": f"hold-{gpu}",
                                           "gpu_ids": [gpu], "model": "x"})
    assert r.status_code == 201, r.text
    return r.json()["lease"]["lease_id"]


def test_split_load_reserves_busy_cards_and_loads_once_they_free(menv, registry, monkeypatch):  # noqa: F811
    import threading
    import time

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    held = _hold(c, 1)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "small:9b", "gpu_ids": [1, 2], "wait_s": 20})))
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not menv["rig"]()["claims"]:
        time.sleep(0.05)
    (claim,) = menv["rig"]()["claims"]
    assert claim["name"] == "wait-small-9b-g1-2" and claim["gpu_ids"] == [1, 2] and claim["expires_at"]
    # The free partner card cannot be taken by single work while the split waits for GPU 1.
    single = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": "solo", "run_id": "s",
                                                "gpu_ids": [2]})
    assert single.status_code == 409 and "wait-small-9b-g1-2" in single.json()["error"]["message"]
    assert c.post(f"/v1/manager/leases/{held}/release", json={"owner": "lanes"}).status_code == 200
    t.join(timeout=20)
    assert out["r"].status_code == 200, out["r"].text
    assert out["r"].json()["gpus"] == [1, 2]
    assert menv["rig"]()["claims"] == []          # the lease holds the cards now; the reservation is gone
    assert [l["gpu_ids"] for l in menv["leases"]()] == [[1, 2]]


def test_a_wait_that_runs_out_drops_its_reservation(menv, registry, monkeypatch):  # noqa: F811
    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    _hold(c, 1)
    r = c.post("/v1/manager/models/load", json={"model": "small:9b", "gpu_ids": [1, 2], "wait_s": 0.4})
    assert r.status_code == 504 and "GPU 1 leased by benchmarks/lanes" in r.json()["error"]["message"]
    assert menv["rig"]()["claims"] == [] and not menv["rig"]().get("plan_items")


def _wait_for(pred, timeout=10):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_load_naming_no_cards_collects_cards_as_they_free(menv, registry, monkeypatch):  # noqa: F811
    import threading

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    held = _hold(c, 1)                       # mid:14b needs both 1060s; GPU 1 is busy, GPU 2 and the 3090 free
    st = menv["rig"]()
    st["gpus"][1]["used_mb"] = 5000          # the busy card shows its model's memory, as on the rig
    menv["write"](st)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "mid:14b", "wait_s": -1})))
    t.start()
    assert _wait_for(lambda: menv["rig"]()["claims"])
    (claim,) = menv["rig"]()["claims"]
    assert claim["name"].startswith("wait-mid-14b-") and claim["gpu_ids"] == [2]   # the free 1060, not the 3090
    single = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": "solo", "run_id": "s",
                                                "gpu_ids": [2]})
    assert single.status_code == 409                                                # held for the waiting load
    assert c.post(f"/v1/manager/leases/{held}/release", json={"owner": "lanes"}).status_code == 200
    st = menv["rig"]()
    st["gpus"][1]["used_mb"] = 4             # its model unloaded with the lease
    menv["write"](st)
    t.join(timeout=20)
    assert out["r"].status_code == 200, out["r"].text
    assert out["r"].json()["gpus"] == [1, 2]
    assert menv["rig"]()["extends"] == [{"name": claim["name"], "gpus": [1]}]       # grown, never re-made
    assert menv["rig"]()["claims"] == []                                            # the lease holds them now
    assert [l["gpu_ids"] for l in menv["leases"]()] == [[1, 2]]


def test_removing_the_claim_cancels_a_collecting_wait(menv, registry, monkeypatch):  # noqa: F811
    import threading

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    _hold(c, 1)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "mid:14b", "wait_s": -1})))
    t.start()
    assert _wait_for(lambda: menv["rig"]()["claims"])
    name = menv["rig"]()["claims"][0]["name"]
    assert c.post(f"/v1/manager/claims/{name}/release").status_code == 200
    t.join(timeout=20)
    assert out["r"].status_code == 409 and "cancelled" in out["r"].json()["error"]["message"]
    assert menv["rig"]()["claims"] == [] and not menv["rig"]().get("plan_items")


def test_removing_the_reservation_cancels_an_unlimited_split_wait(menv, registry, monkeypatch):  # noqa: F811
    import threading

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    _hold(c, 1)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "small:9b", "gpu_ids": [1, 2], "wait_s": -1})))
    t.start()
    assert _wait_for(lambda: menv["rig"]()["claims"])
    assert c.post("/v1/manager/claims/wait-small-9b-g1-2/release").status_code == 200
    t.join(timeout=20)
    assert out["r"].status_code == 409 and "cancelled" in out["r"].json()["error"]["message"]
    assert not menv["rig"]().get("plan_items")


def test_without_wait_a_busy_card_still_fails_at_once(menv, registry):  # noqa: F811
    c = menv["client"]
    _hold(c, 1)
    r = c.post("/v1/manager/models/load", json={"model": "small:9b", "gpu_ids": [1, 2]})
    assert r.status_code == 409 and menv["rig"]()["claims"] == []


def test_kill_stops_runtimes_releases_leases_and_drops_claims(menv):  # noqa: F811
    c = menv["client"]
    lid = _hold(c, 2)
    st = menv["rig"]()
    st["containers"] = [{"name": "llama-lane-gpu2", "gpus": [2], "port": 11436}]
    menv["write"](st)
    assert c.post("/v1/manager/claims", json={"name": "pair", "gpu_ids": [1, 2], "owner": "lane-1-2",
                                              "ttl_s": 600}).status_code == 201
    r = c.post("/v1/manager/kill", json={"gpu_ids": [2], "reason": "test"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["stopped"] == ["llama-lane-gpu2"] and out["released"] == [lid] and out["claims_dropped"] == ["pair"]
    st = menv["rig"]()
    assert st["leases"] == [] and st["claims"] == [] and st["containers"] == []
    events = c.get("/v1/manager/status").json()["rig"]["events"]
    assert any("kill GPU [2] (test)" in e["event"] for e in events)


def test_force_load_kills_what_runs_on_the_cards_first(menv, registry):  # noqa: F811
    c = menv["client"]
    _hold(c, 1, owner="someone-else")
    r = c.post("/v1/manager/models/load", json={"model": "small:9b", "gpu_ids": [1, 2], "force": True})
    assert r.status_code == 200, r.text
    assert [l["owner"] for l in menv["leases"]()] == ["llm-gateway"]


def test_claims_carry_owner_and_ttl_and_renew(menv):  # noqa: F811
    c = menv["client"]
    r = c.post("/v1/manager/claims", json={"name": "lane-4-5", "gpu_ids": [1, 2], "owner": "lane", "ttl_s": 600,
                                           "purpose": "benchmark lane"})
    assert r.status_code == 201 and r.json()["claim"]["expires_at"]
    assert c.post("/v1/manager/claims/lane-4-5/renew", json={"owner": "lane", "ttl_s": 600}).status_code == 200
    row = {g["gpu"]: g for g in c.get("/v1/manager/status").json()["gpus"]}[1]
    assert row["claim"] == "lane-4-5" and row["claim_info"]["owner"] == "lane"
    assert c.post("/v1/manager/claims/nope/renew", json={}).status_code == 404


# ------------------------------------------------------------------ runs, cancel by owner, outages, demand

def test_a_run_is_a_job_in_the_report_and_cancel_owner_cleans_up_what_it_holds(menv):
    c = menv["client"]
    r = c.post("/v1/manager/runs", json={"run": "camp-1", "argv": ["python3", "run_campaign.py", "m.json"],
                                          "resume_argv": ["python3", "run_campaign.py", "m.json", "--run-id", "camp-1"]})
    assert r.status_code == 201, r.text
    (run,) = c.get("/v1/manager/runs").json()["runs"]
    assert run["run"] == "camp-1" and run["state"] == "running" and run["owner"] == "camp-1"
    assert c.post("/v1/manager/runs", json={"run": "camp-1", "argv": ["x"]}).status_code == 409   # not twice
    lease = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": "campaign-gpu_1", "run_id": "camp-1",
                                                "gpu_ids": [1]}).json()["lease"]["lease_id"]
    assert c.post("/v1/manager/claims", json={"name": "campaign-camp-1-gpu_2", "gpu_ids": [2],
                                              "owner": "campaign-camp-1"}).status_code == 201
    out = c.post("/v1/manager/owners/camp-1/cancel", json={"reason": "test"}).json()
    assert out["runs_stopped"] == ["camp-1"] and out["leases_released"] == [lease]
    assert out["claims_dropped"] == ["campaign-camp-1-gpu_2"] and not out["errors"]
    st = menv["rig"]()
    assert st["leases"] == [] and st["claims"] == [] and st["runs"][0]["state"] == "stopped"
    events = c.get("/v1/manager/status").json()["rig"]["events"]
    assert any(e["event"].startswith("cancel camp-1 (test)") for e in events)


def test_an_interrupted_run_is_offered_then_resumed_from_its_checkpoint(menv):
    c = menv["client"]
    resume = ["python3", "run_campaign.py", "m.json", "--run-id", "camp-2"]
    c.post("/v1/manager/runs", json={"run": "camp-2", "argv": ["python3", "run_campaign.py", "m.json"],
                                     "resume_argv": resume})
    st = menv["rig"]()
    st["runs"][0].update(state="interrupted", ended_at="2026-10-22T20:00:00Z", result="interrupted: the rig restarted")
    menv["write"](st)
    assert c.post("/v1/manager/reconcile").status_code == 200
    events = c.get("/v1/manager/status").json()["rig"]["events"]
    assert any("rigctl recover camp-2" in e["event"] for e in events)
    assert menv["rig"]()["runs"][0]["state"] == "interrupted"            # offered, not restarted on its own
    r = c.post("/v1/manager/runs/camp-2/recover")
    assert r.status_code == 200, r.text
    (run,) = menv["rig"]()["runs"]
    assert run["state"] == "running" and run["argv"] == resume and run["attempts"] == 2
    assert c.post("/v1/manager/runs/camp-2/recover").status_code == 409  # running: nothing to resume


def test_a_run_that_opted_in_resumes_on_its_own(menv):
    c = menv["client"]
    c.post("/v1/manager/runs", json={"run": "night", "argv": ["a"], "resume_argv": ["a", "--resume"], "auto_resume": True})
    st = menv["rig"]()
    st["runs"][0].update(state="interrupted", ended_at="2026-10-22T20:00:00Z")
    menv["write"](st)
    c.post("/v1/manager/reconcile")
    (run,) = menv["rig"]()["runs"]
    assert run["state"] == "running" and run["argv"] == ["a", "--resume"]


def test_dismissing_an_ended_run_cleans_up_and_a_running_one_is_refused(menv):
    c = menv["client"]
    c.post("/v1/manager/runs", json={"run": "lane-x", "argv": ["a"]})
    assert c.post("/v1/manager/runs/lane-x/dismiss").status_code == 409
    st = menv["rig"]()
    st["runs"][0]["state"] = "failed"
    menv["write"](st)
    out = c.post("/v1/manager/runs/lane-x/dismiss").json()
    assert out["run"]["state"] == "dismissed" and out["cleanup"]["owner"] == "lane-x"


def test_a_small_model_does_not_take_a_card_a_run_is_waiting_for(menv, registry):  # noqa: F811
    c = menv["client"]
    _hold(c, 1)
    _hold(c, 2)                              # both 1060s busy: small:9b would spill onto the 3090
    c.post("/v1/manager/runs", json={"run": "brain-q", "argv": ["a"], "needs": [{"card_mb": 24564, "count": 1}]})
    r = c.post("/v1/manager/models/load", json={"model": "small:9b"})
    assert r.status_code == 409 and "kept free" in r.json()["error"]["message"]
    r = c.post("/v1/manager/models/load", json={"model": "small:9b", "owner": "brain-q"})   # its own need
    assert r.status_code == 200, r.text
    assert r.json()["gpus"] == [0]


def test_a_collecting_wait_rides_out_an_unreachable_rig(menv, registry, monkeypatch):  # noqa: F811
    import os
    import threading

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    held = _hold(c, 1)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "mid:14b", "wait_s": -1, "owner": "lane-z"})))
    t.start()
    assert _wait_for(lambda: menv["rig"]()["claims"])
    down = menv["state"] + ".down"
    open(down, "w").close()                  # the rig drops off the network
    import time
    time.sleep(0.6)
    os.remove(down)                          # and comes back
    assert c.post(f"/v1/manager/leases/{held}/release", json={"owner": "lanes"}).status_code == 200
    t.join(timeout=20)
    assert out["r"].status_code == 200, out["r"].text
    events = [e["event"] for e in c.get("/v1/manager/status").json()["rig"]["events"]]
    assert any("rig unreachable; still waiting" in e for e in events)
    assert any("rig back after" in e for e in events)
    (lease,) = [l for l in menv["leases"]() if l["gpu_ids"] == [1, 2]]
    assert lease["metadata"]["requested_by"] == "lane-z"


def test_cancel_owner_stops_its_collecting_wait(menv, registry, monkeypatch):  # noqa: F811
    import threading

    from llm_gateway.manager import core
    monkeypatch.setattr(core, "WAIT_POLL_S", 0.1)
    c = menv["client"]
    _hold(c, 1)
    out = {}
    t = threading.Thread(target=lambda: out.update(r=c.post("/v1/manager/models/load", json={
        "model": "mid:14b", "wait_s": -1, "owner": "lane-q"})))
    t.start()
    assert _wait_for(lambda: menv["rig"]()["claims"])
    cleanup = c.post("/v1/manager/owners/lane-q/cancel").json()
    assert cleanup["claims_dropped"] and not cleanup["errors"]
    t.join(timeout=20)
    assert out["r"].status_code == 409 and "cancelled" in out["r"].json()["error"]["message"]
