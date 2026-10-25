"""Shared test setup: no test writes to the manager's real fit observation log."""

from __future__ import annotations

import pytest

from llm_gateway.manager import fit


@pytest.fixture(autouse=True)
def fit_log(tmp_path, monkeypatch):
    path = tmp_path / "fit_observations.jsonl"
    monkeypatch.setattr(fit, "FIT_LOG_PATH", str(path))
    return path
