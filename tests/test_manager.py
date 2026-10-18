"""Rig manager: placement and status rules, plus the manager end to end against a fake `rig`."""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app
from llm_gateway.manager import core
from llm_gateway.manager.placement import availability, place, port_conflict, port_for
from llm_gateway.manager.status import gpu_rows
from llm_gateway.settings import Settings

GPUS = [{"index": 0, "name": "3090", "total_mb": 24564, "used_mb": 15},
        {"index": 1, "name": "1060", "total_mb": 6144, "used_mb": 4},
        {"index": 2, "name": "1060", "total_mb": 6144, "used_mb": 4}]
SMALL = {"min_vram_mb": 5800, "max_vram_mb": 8192}
BIG = {"min_vram_mb": 20000}


def obs(**extra):
    return {"gpus": [dict(g) for g in GPUS], "containers": [], "ports": {}, "profiles": {}, "jobs": [], **extra}


# ------------------------------------------------------------------ placement and status (pure)

def test_smallest_fit_and_max_vram_keep_small_jobs_off_the_3090():
    free, _ = availability(obs(), [], [])
    assert place([SMALL], free) == [1]
    assert place([BIG], free) == [0]
    assert place([SMALL, SMALL, SMALL], free) is None  # only two 1060s; the 3090 is above max
    assert port_for([2, 1]) == 11435


def test_unleased_runtime_and_used_vram_make_a_card_unavailable():
    o = obs(containers=[{"name": "llama-lane-gpu1", "gpus": [1], "port": 11435}])
    o["gpus"][2]["used_mb"] = 3000
    free, why = availability(o, [], [])
    assert [g["index"] for g in free] == [0]
    assert "container llama-lane-gpu1" in why[1] and "3000 MB" in why[2]


def test_claims_exclude_cards_unless_named():
    claims = [{"name": "roo", "gpu_ids": [1]}]
    assert [g["index"] for g in availability(obs(), [], claims)[0]] == [0, 2]
    assert [g["index"] for g in availability(obs(), [], claims, claim="roo")[0]] == [1]
    with pytest.raises(KeyError):
        availability(obs(), [], claims, claim="nope")


def test_port_conflicts():
    assert "serving" in port_conflict(11435, obs(ports={"11435": "x.gguf"}), [])
    assert "reserved" in port_conflict(11435, obs(), [{"lease_id": "l", "ports": [11435]}])
    assert port_conflict(11434, obs(), []) is None


def test_status_verdicts():
    now = datetime.now(timezone.utc)
    future = (now + timedelta(minutes=5)).isoformat()
    past = (now - timedelta(minutes=5)).isoformat()
    o = obs(containers=[{"name": "a", "gpus": [0], "port": 11434}, {"name": "b", "gpus": [2], "port": 11436}],
            ports={"11434": "m.gguf"})
    leases = [{"lease_id": "l0", "gpu_ids": [0], "ports": [11434], "expires_at": future, "metadata": {"port": 11434}},
              {"lease_id": "l1", "gpu_ids": [1], "ports": [11435], "expires_at": past, "metadata": {"port": 11435}}]
    rows = {r["gpu"]: r["verdict"] for r in gpu_rows(o, leases, [], now)}
    assert rows == {0: "leased", 1: "stale", 2: "unleased_busy"}


# ------------------------------------------------------------------ manager end to end (fake rig)

FAKE_RIG = textwrap.dedent("""\
    import json, os, sys, time
    path = os.environ["FAKE_RIG_STATE"]
    st = json.load(open(path))
    a = sys.argv[1:]
    def save(): json.dump(st, open(path, "w"))
    def refresh():
        for p in st["profiles"].values():
            if p["state"] == "loading" and time.time() >= p["ready_at"]:
                p["state"] = "ready"
    refresh(); save()
    def fail(kind, msg):
        print(json.dumps({"error": msg, "kind": kind})); sys.exit(1)
    def opts(xs):
        return dict(zip(xs[0::2], xs[1::2]))
    if a[:2] == ["lease", "list"]:
        print(json.dumps({"leases": st["leases"], "claims": st["claims"]})); sys.exit(0)
    if a[:2] == ["lease", "acquire"]:
        spec = json.loads(a[3])
        gpus = spec["gpu_ids"]
        for l in st["leases"]:
            if set(gpus) & set(l["gpu_ids"]):
                fail("conflict", f"GPU(s) {gpus} held by lease {l['lease_id']} ({l['controller']}/{l['owner']}, active)")
        for c in st["claims"]:
            if set(gpus) & set(c["gpu_ids"]) and (spec.get("metadata") or {}).get("claim") != c["name"]:
                fail("conflict", f"GPU {gpus} claimed by {c['name']}")
        now = time.time()
        lease = {"lease_id": f"{spec['controller']}-{len(st['leases'])}-{int(now*1000)}", "controller": spec["controller"],
                 "owner": spec["owner"], "run_id": spec["run_id"], "gpu_ids": gpus, "ports": spec["ports"],
                 "metadata": spec.get("metadata") or {},
                 "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                 "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + spec["ttl_s"]))}
        st["leases"].append(lease); save(); print(json.dumps({"lease": lease})); sys.exit(0)
    if a[:2] in (["lease", "renew"], ["lease", "release"]):
        o = opts(a[3:]); match = [l for l in st["leases"] if l["lease_id"] == a[2]]
        if not match:
            fail("not_found", f"lease not found: {a[2]}")
        if o.get("--owner") != match[0]["owner"] and "--force" not in a:
            fail("ownership", f"lease {a[2]} is owned by {match[0]['owner']}, not {o.get('--owner')}")
        if a[1] == "renew":
            match[0]["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(o["--ttl"])))
            save(); print(json.dumps({"lease": match[0]})); sys.exit(0)
        st["leases"].remove(match[0]); save(); print(json.dumps({"released": a[2]})); sys.exit(0)
    if a[:2] == ["claim", "add"]:
        o = opts(a[3:]); gpus = [int(g) for g in o["--gpus"].split(",")]
        for c in st["claims"]:
            if c["name"] == a[2] or set(gpus) & set(c["gpu_ids"]):
                fail("conflict", f"GPU {gpus} already claimed by {c['name']}")
        claim = {"name": a[2], "gpu_ids": gpus, "note": o.get("--note", "")}
        st["claims"].append(claim); save(); print(json.dumps({"claim": claim})); sys.exit(0)
    if a[:2] == ["claim", "remove"]:
        match = [c for c in st["claims"] if c["name"] == a[2]]
        if not match:
            fail("not_found", f"no claim {a[2]}")
        st["claims"].remove(match[0]); save(); print(json.dumps({"released": match[0]})); sys.exit(0)
    if a == ["observe"]:
        ctrs = list(st["containers"]) + [{"name": "rig-remote-" + k, "gpus": p["gpus"], "port": p["port"]}
                                         for k, p in st["profiles"].items()]
        ports = {str(p["port"]): k + ".gguf" for k, p in st["profiles"].items() if p["state"] == "ready"}
        print(json.dumps({"observed_at": "now", "mode": "remote" if st["profiles"] else "idle",
                          "gpus": st["gpus"], "containers": ctrs, "ports": ports, "profiles": st["profiles"],
                          "jobs": st["jobs"], "last_error": st.get("last_error"),
                          "leases": st["leases"], "claims": st["claims"],
                          "guard": st.get("guard", {"level": "ok", "mem_available_mb": 20000})})); sys.exit(0)
    if a[:2] == ["status", "--json"]:
        print(json.dumps({"mode": "remote", "profiles": [{"id": k, **p} for k, p in st["profiles"].items()]}))
        sys.exit(0)
    if a[:2] == ["start", "remote"] and "--detach" in a:
        pid = a[2]; opt = dict(zip(a[3::2], a[4::2]))
        mode = os.environ.get("FAKE_DETACH", "ok")
        st.setdefault("starts", []).append({"profile": pid, "gpus": opt["--gpus"], "port": opt["--port"],
                                            "lease": opt["--lease"], "cache_ram": opt.get("--cache-ram")})
        if mode == "refuse":
            save(); print("error: manager placed it on a busy GPU"); sys.exit(4)
        if mode == "fail":
            st["jobs"].append({"job": "rig-load-" + pid, "active_state": "failed",
                               "log_tail": "boom: cudaMalloc failed: out of memory"})
        else:
            st["profiles"][pid] = {"port": int(opt["--port"]), "gpus": [int(g) for g in opt["--gpus"].split(",")],
                                   "state": "loading", "lease": opt["--lease"], "ready_at": time.time() + 0.5}
        save(); print(json.dumps({"job": "rig-load-" + pid, "active_state": "active", "already_running": False}))
        sys.exit(0)
    if a[0] == "stop":
        st["profiles"].pop(a[1], None); save(); print("unloaded"); sys.exit(0)
    sys.exit(64)
""")


def any_llama(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "port": request.url.port})


@pytest.fixture
def menv(tmp_path, monkeypatch):
    (tmp_path / "rig.py").write_text(FAKE_RIG)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"gpus": GPUS, "containers": [], "profiles": {}, "jobs": [],
                                 "leases": [], "claims": []}))
    profiles = tmp_path / "remote_profiles.json"
    profiles.write_text(json.dumps({"remote_profiles": [
        {"id": "small", "model": "s:7b", "ctx_size": 16384, "gpus_needed": [SMALL]},
        {"id": "big", "model": "b:20b", "ctx_size": 65536, "gpus_needed": [BIG]},
        {"id": "worker", "model": "w:7b", "ctx_size": 8192, "gpus_needed": [SMALL], "use": "worker"},
    ]}))
    monkeypatch.setenv("FAKE_RIG_STATE", str(state))
    monkeypatch.setattr(core, "LOAD_POLL_S", 0.1)
    monkeypatch.setattr(core, "OBSERVE_CACHE_S", 0.0)
    settings = Settings(
        rig_command=(sys.executable, str(tmp_path / "rig.py")), profiles_path=profiles,
        upstream_host="127.0.0.1", model_prefix="gpu-rig", bind="127.0.0.1", port=0,
        start_timeout_seconds=30, request_timeout_seconds=30, manager_enabled=True,
    )
    app = create_app(settings, upstream_transport=httpx.MockTransport(any_llama), run_reconciler=False)

    def rig_state():
        return json.loads(state.read_text())

    def write_state(st):
        state.write_text(json.dumps(st))

    def leases():
        return rig_state()["leases"]

    with TestClient(app) as c:
        yield {"client": c, "rig": rig_state, "write": write_state, "leases": leases, "app": app}


def chat(c, model):
    return c.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def test_cold_request_takes_a_lease_and_hands_the_rig_one_job(menv):
    r = chat(menv["client"], "gpu-rig/small")
    assert r.status_code == 200 and r.json()["port"] == 11435
    (lease,) = menv["leases"]()
    assert lease["controller"] == "interactive" and lease["gpu_ids"] == [1]
    assert lease["metadata"]["port"] == 11435 and lease["ports"] == [11435]
    (start,) = menv["rig"]()["starts"]
    assert start == {"profile": "small", "gpus": "1", "port": "11435", "lease": lease["lease_id"], "cache_ram": "4096"}
    assert chat(menv["client"], "gpu-rig/small").status_code == 200  # warm: no second job
    assert len(menv["rig"]()["starts"]) == 1


def test_cards_busy_without_a_lease_are_skipped(menv):
    st = menv["rig"]()
    st["containers"] = [{"name": "llama-lane-gpu1", "gpus": [1], "port": 11435}]
    menv["write"](st)
    assert chat(menv["client"], "gpu-rig/small").json()["port"] == 11436
    rows = {g["gpu"]: g["verdict"] for g in menv["client"].get("/v1/manager/status").json()["gpus"]}
    assert rows[1] == "unleased_busy" and rows[2] == "leased"


def test_claimed_cards_leave_the_pool(menv):
    c = menv["client"]
    assert c.post("/v1/manager/claims", json={"name": "bench", "gpu_ids": [1, 2]}).status_code == 201
    r = chat(c, "gpu-rig/small")
    assert r.status_code == 503 and "no capacity" in r.json()["error"]["message"]
    assert c.post("/v1/manager/profiles/small/load", json={"claim": "bench"}).json()["port"] == 11435
    assert c.post("/v1/manager/claims", json={"name": "x", "gpu_ids": [2]}).status_code == 409
    assert c.post("/v1/manager/claims/bench/release").status_code == 200


def test_failed_rig_job_releases_the_lease_and_reports_the_log(menv, monkeypatch):
    monkeypatch.setenv("FAKE_DETACH", "fail")
    r = chat(menv["client"], "gpu-rig/small")
    assert r.status_code == 502 and "out of memory" in r.json()["error"]["message"]
    assert menv["leases"]() == []


def test_rig_refusal_releases_the_lease(menv, monkeypatch):
    monkeypatch.setenv("FAKE_DETACH", "refuse")
    r = chat(menv["client"], "gpu-rig/small")
    assert r.status_code == 503 and "busy GPU" in r.json()["error"]["message"]
    assert menv["leases"]() == []


def test_explicit_gpus_conflict_names_the_holder(menv):
    c = menv["client"]
    body = {"controller": "benchmarks", "owner": "lanes", "run_id": "r1", "gpu_ids": [0], "model": "x"}
    first = c.post("/v1/manager/leases", json=body)
    assert first.status_code == 201 and first.json()["port"] == 11434
    second = c.post("/v1/manager/leases", json={**body, "run_id": "r2"})
    assert second.status_code == 409 and "benchmarks/lanes" in second.json()["error"]["message"]
    r = chat(c, "gpu-rig/big")
    assert r.status_code == 503
    lid = first.json()["lease"]["lease_id"]
    assert c.post(f"/v1/manager/leases/{lid}/release", json={"owner": "someone"}).status_code == 403
    assert c.post(f"/v1/manager/leases/{lid}/release", json={"owner": "lanes"}).status_code == 200


def test_release_refuses_while_a_runtime_runs(menv):
    c = menv["client"]
    lid = c.post("/v1/manager/leases", json={"controller": "benchmarks", "owner": "lanes", "run_id": "r",
                                             "gpu_ids": [2]}).json()["lease"]["lease_id"]
    st = menv["rig"]()
    st["containers"] = [{"name": "llama-lane-gpu2", "gpus": [2], "port": 11436}]
    menv["write"](st)
    r = c.post(f"/v1/manager/leases/{lid}/release", json={"owner": "lanes"})
    assert r.status_code == 409 and "llama-lane-gpu2" in r.json()["error"]["message"]


def test_unload_releases_the_lease(menv):
    c = menv["client"]
    assert chat(c, "gpu-rig/small").status_code == 200
    out = c.post("/v1/manager/profiles/small/unload").json()
    assert out["unloaded"] == "small" and out["released"]
    assert menv["leases"]() == []


def test_reconcile_renews_backed_and_releases_orphaned_leases(menv, monkeypatch):
    c = menv["client"]
    assert chat(c, "gpu-rig/small").status_code == 200
    orphan = c.post("/v1/manager/leases", json={"controller": "interactive", "owner": core.OWNER,
                                                "run_id": "profile-gone", "gpus_needed": [SMALL]}).json()["lease"]
    monkeypatch.setattr(core, "ORPHAN_GRACE_S", 0)
    out = c.post("/v1/manager/reconcile").json()
    assert out["released"] == [orphan["lease_id"]] and len(out["renewed"]) == 1
    assert [l["run_id"] for l in menv["leases"]()] == ["profile-small"]


def test_cache_follows_the_use_and_the_rig_free_ram(menv):
    from llm_gateway.manager.memory import cache_ram_mib
    assert cache_ram_mib("chat", 20000) == 4096
    assert cache_ram_mib("chat", 10000) == 1792      # bounded by what is left above the reserve
    assert cache_ram_mib("chat", 6000) == 0
    assert cache_ram_mib("chat", None) == 0
    assert cache_ram_mib("worker", 30000) == 0 and cache_ram_mib("benchmark", 30000) == 0
    c = menv["client"]
    assert chat(c, "gpu-rig/worker").status_code == 200
    st = menv["rig"]()
    st["guard"] = {"level": "ok", "mem_available_mb": 9000}
    menv["write"](st)
    assert chat(c, "gpu-rig/small").status_code == 200
    starts = {s["profile"]: s["cache_ram"] for s in menv["rig"]()["starts"]}
    assert starts == {"worker": "0", "small": "768"}


def test_gone_runtime_is_cleared_and_its_lease_released(menv, monkeypatch):
    c = menv["client"]
    assert chat(c, "gpu-rig/small").status_code == 200
    st = menv["rig"]()
    st["profiles"]["small"]["state"] = "gone"   # the rig guard stopped it
    menv["write"](st)
    monkeypatch.setattr(core, "ORPHAN_GRACE_S", 0)
    out = c.post("/v1/manager/reconcile").json()
    assert len(out["released"]) == 1
    assert menv["rig"]()["profiles"] == {} and menv["leases"]() == []
