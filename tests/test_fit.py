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
    (root / "model_registry.json").write_text(json.dumps({"models": {
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
