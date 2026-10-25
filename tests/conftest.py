"""Shared test setup: no test reads or writes the real benchmarking history (load observations, capacity records)."""

from __future__ import annotations

import pytest

from llm_gateway.manager import observations


@pytest.fixture(autouse=True)
def observation_log(tmp_path, monkeypatch):
    path = tmp_path / "load_observations.jsonl"
    monkeypatch.setattr(observations, "PATH", str(path))
    monkeypatch.setattr(observations, "results_dir", lambda: None)
    return path
