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
def build(offer):
    return gen.build(offer, LIBRARY, REGISTRY)


def test_only_offered_models_are_resolved_with_registry_settings():
    doc, problems = build([{"id": "s", "model": "small:7b", "profile": "interactive"}])
    assert problems == []
    (p,) = doc["remote_profiles"]
    assert p["id"] == "s" and p["model"] == "small:7b" and p["registry_profile"] == "interactive"
    assert p["ctx_size"] == 16384 and doc["registry_updated"] == "2026-10-17"
    assert not {"gpus_needed", "tensor_split", "gguf_path", "image", "extra_args"} & set(p)  # the catalog has those


def test_candidates_are_offerable_archived_are_not():
    doc, problems = build([{"id": "c", "model": "split:35b"}, {"id": "a", "model": "old:8b"}])
    assert [p["id"] for p in doc["remote_profiles"]] == ["c"]
    assert any("a: old:8b is archived" in p for p in problems), problems


def test_bad_offers_are_reported_and_left_out():
    doc, problems = build([
        {"id": "u", "model": "nope:1b"},
        {"id": "m", "model": "small:7b", "profile": "missing"},
        {"id": "g", "model": "small:7b", "gpus_needed": [{"min_vram_mb": 5800}]},  # the offer names no cards
        {"id": "ok", "model": "small:7b"},
        {"id": "ok", "model": "small:7b"},
    ])
    assert [p["id"] for p in doc["remote_profiles"]] == ["ok"]
    assert len(problems) == 4, problems


def test_gateway_reads_the_generated_file(tmp_path):
    doc, _ = build([{"id": "s", "model": "small:7b"}])
    path = tmp_path / "remote_profiles.json"
    path.write_text(gen.render(doc))
    assert Profiles(path).all()["s"]["ctx_size"] == 16384


def test_the_offer_in_the_script_is_well_formed():
    ids = [o["id"] for o in gen.OFFER]
    assert len(ids) == len(set(ids))
    for o in gen.OFFER:  # the offer names no cards: the fit places each published profile
        assert "gpus_needed" not in o and "tensor_split" not in o, o["id"]


def test_committed_file_matches_the_offer_ids():
    committed = json.loads((gen.OUTPUT_PATH).read_text(encoding="utf-8"))
    assert [p["id"] for p in committed["remote_profiles"]] == [o["id"] for o in gen.OFFER]


def test_use_is_carried_and_checked():
    doc, problems = build([{"id": "w", "model": "small:7b", "use": "worker"}])
    assert problems == [] and doc["remote_profiles"][0]["use"] == "worker"
    _, problems = build([{"id": "x", "model": "small:7b", "use": "party"}])
    assert any("use must be one of" in p for p in problems), problems


def test_the_catalog_copies_every_model_with_resolved_profiles_and_the_roles():
    reg = {**REGISTRY, "models": {"small:7b": {"status": "active", "gguf": "small/small.gguf", "aliases": ["s7"],
                                               "profiles": {"default": {}, "interactive": {}}}}}
    roles = {"updated": "2026-10-15", "roles": {"brain": {"model": "small:7b", "lane": "3090"}}}
    cat = gen.build_catalog(LIBRARY, reg, roles, None)
    m = cat["models"]["small:7b"]
    assert m["gguf_path"] == "/archive/small/small.gguf" and m["aliases"] == ["s7"]
    assert sorted(m["profiles"]) == ["default", "interactive"]
    assert m["profiles"]["default"]["runtime"]["ctx_size"] == 16384 and "gguf_path" not in m["profiles"]["default"]
    assert cat["image"] == "llama-runtime:test"
    assert cat["roles"]["brain"]["model"] == "small:7b" and cat["roles_updated"] == "2026-10-15"


def test_the_manager_resolves_from_the_catalog(tmp_path, monkeypatch):
    from llm_gateway.manager import models
    reg = {**REGISTRY, "models": {"small:7b": {"status": "active", "gguf": "small/small.gguf", "aliases": ["s7"],
                                               "profiles": {"default": {}, "interactive": {}}}}}
    path = tmp_path / "model_catalog.json"
    path.write_text(gen.render(gen.build_catalog(LIBRARY, reg, None, None)))
    monkeypatch.setenv("MODEL_CATALOG", str(path))
    r = models.resolve("s7", "chat")  # by alias, the chat use's profile
    assert r["model"] == "small:7b" and r["profile"] == "interactive"
    assert r["gguf_path"] == "/archive/small/small.gguf" and r["image"] == "llama-runtime:test"
    assert r["runtime"]["batch_size"] == 64
