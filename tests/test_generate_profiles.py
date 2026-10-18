"""tools/generate_profiles.py: the offer resolved against a stub model library."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from llm_gateway.profiles import Profiles

spec = importlib.util.spec_from_file_location(
    "generate_profiles", Path(__file__).resolve().parents[1] / "tools" / "generate_profiles.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

REGISTRY = {
    "updated": "2026-10-17",
    "runtime_image": "llama-runtime:test",
    "models": {
        "small:7b": {"status": "active", "gguf": "small/small.gguf"},
        "split:35b": {"status": "candidate", "gguf": "split/split.gguf"},
        "old:8b": {"status": "archived", "gguf": "old/old.gguf"},
    },
}


def resolve(model_id, profile, registry):
    entry = registry["models"].get(model_id)
    if entry is None:
        raise KeyError(f"model {model_id} is not in model_registry.json")
    if profile == "missing":
        raise KeyError(f"model {model_id} has no profile missing")
    return {"model": model_id, "profile": profile, "status": entry["status"],
            "gguf_path": f"/archive/{entry['gguf']}", "extra_args": ["--jinja"],
            "runtime": {"ctx_size": 16384, "batch_size": 64, "parallel": 1}}


LIBRARY = SimpleNamespace(resolve=resolve, runtime_image=lambda registry: registry["runtime_image"])
GPU = {"min_vram_mb": 5800}


def build(offer):
    return gen.build(offer, LIBRARY, REGISTRY)


def test_only_offered_models_are_resolved_with_registry_settings():
    doc, problems = build([{"id": "s", "model": "small:7b", "profile": "interactive", "gpus_needed": [GPU]}])
    assert problems == []
    (p,) = doc["remote_profiles"]
    assert p["id"] == "s" and p["model"] == "small:7b" and p["registry_profile"] == "interactive"
    assert p["ctx_size"] == 16384 and p["gguf_path"] == "/archive/small/small.gguf"
    assert p["extra_args"] == ["--jinja"] and p["image"] == "llama-runtime:test"
    assert doc["registry_updated"] == "2026-10-17"


def test_candidates_are_offerable_archived_are_not():
    doc, problems = build([
        {"id": "c", "model": "split:35b", "gpus_needed": [GPU]},
        {"id": "a", "model": "old:8b", "gpus_needed": [GPU]},
    ])
    assert [p["id"] for p in doc["remote_profiles"]] == ["c"]
    assert any("a: old:8b is archived" in p for p in problems), problems


def test_tensor_split_needs_one_weight_per_gpu():
    _, problems = build([{"id": "x", "model": "split:35b", "gpus_needed": [GPU] * 5, "tensor_split": "1,1,1,1"}])
    assert any("one weight per gpus_needed entry (5)" in p for p in problems), problems
    doc, problems = build([{"id": "x", "model": "split:35b", "gpus_needed": [GPU] * 2, "tensor_split": "0.75,1"}])
    assert problems == [] and doc["remote_profiles"][0]["tensor_split"] == "0.75,1"


def test_bad_offers_are_reported_and_left_out():
    doc, problems = build([
        {"id": "u", "model": "nope:1b", "gpus_needed": [GPU]},
        {"id": "m", "model": "small:7b", "profile": "missing", "gpus_needed": [GPU]},
        {"id": "g", "model": "small:7b"},
        {"id": "ok", "model": "small:7b", "gpus_needed": [GPU]},
        {"id": "ok", "model": "small:7b", "gpus_needed": [GPU]},
    ])
    assert [p["id"] for p in doc["remote_profiles"]] == ["ok"]
    assert len(problems) == 4, problems


def test_gateway_reads_the_generated_file(tmp_path):
    doc, _ = build([{"id": "s", "model": "small:7b", "gpus_needed": [GPU]}])
    path = tmp_path / "remote_profiles.json"
    path.write_text(gen.render(doc))
    assert Profiles(path).all()["s"]["ctx_size"] == 16384


def test_the_offer_in_the_script_is_well_formed():
    ids = [o["id"] for o in gen.OFFER]
    assert len(ids) == len(set(ids))
    for o in gen.OFFER:
        assert o["gpus_needed"], o["id"]
        if o.get("tensor_split"):
            assert len(o["tensor_split"].split(",")) == len(o["gpus_needed"]), o["id"]


def test_committed_file_matches_the_offer_ids():
    committed = json.loads((gen.OUTPUT_PATH).read_text(encoding="utf-8"))
    assert [p["id"] for p in committed["remote_profiles"]] == [o["id"] for o in gen.OFFER]


def test_gpu_range_must_be_ordered():
    bad = {"min_vram_mb": 8000, "max_vram_mb": 6000}
    _, problems = build([{"id": "r", "model": "small:7b", "gpus_needed": [bad]}])
    assert any("max_vram_mb below min_vram_mb" in p for p in problems), problems


def test_1060_slot_excludes_the_3090():
    assert gen.ONE_1060["max_vram_mb"] < gen.ONE_3090["min_vram_mb"]
