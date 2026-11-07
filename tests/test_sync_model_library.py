"""Focused checks for the benchmarking to Manager import report."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
spec = importlib.util.spec_from_file_location(
    "sync_model_library", Path(__file__).resolve().parents[1] / "tools" / "sync_model_library.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def test_changed_keys_reports_add_remove_and_content_change():
    assert sync.changed_keys({"same": 1, "old": 1, "edit": 1},
                             {"same": 1, "new": 1, "edit": 2}) == {
        "added": ["new"], "removed": ["old"], "changed": ["edit"]}


def test_inspect_ignores_summary_build_time_but_detects_profile_change(tmp_path, monkeypatch):
    root = tmp_path / "benchmarking"
    (root / "scripts/active").mkdir(parents=True)
    (root / "model_registry.json").write_text("{}")
    (root / "model_roles.json").write_text("{}")
    (root / "scripts/active/model_library.py").write_text("# converter")
    catalog_path = tmp_path / "model_catalog.json"
    offer_path = tmp_path / "remote_profiles.json"
    old_catalog = {"models": {"a": {"profiles": {"default": {"runtime": {"ctx_size": 4096}}}}},
                   "roles": {}, "summary": {"generated_at": "old"}, "image": "llama-runtime:x"}
    catalog_path.write_text(json.dumps(old_catalog))
    offer_path.write_text(json.dumps({"remote_profiles": [{"id": "a", "ctx_size": 4096}]}))
    monkeypatch.setattr(sync.gen, "CATALOG_PATH", catalog_path)
    monkeypatch.setattr(sync.gen, "OUTPUT_PATH", offer_path)
    fresh = {**old_catalog, "summary": {"generated_at": "new"}}
    report = sync.inspect(root, fresh, {"remote_profiles": [{"id": "a", "ctx_size": 4096}]})
    assert not report["catalog_stale"] and not report["offers_stale"]
    fresh["models"]["a"]["profiles"]["default"]["runtime"]["ctx_size"] = 8192
    report = sync.inspect(root, fresh, {"remote_profiles": [{"id": "a", "ctx_size": 8192}]})
    assert report["catalog_stale"] and report["offers_stale"]
    assert report["models"]["changed"] == ["a"]
    assert report["offers"]["changed"] == ["a"]


def test_atomic_text_replaces_complete_file(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text("old")
    sync._atomic_text(path, "new\n")
    assert path.read_text() == "new\n"
    assert list(tmp_path.iterdir()) == [path]
