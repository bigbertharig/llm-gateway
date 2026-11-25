import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3


from llm_gateway import reset_operation as reset


def saved_codex_chat(root: Path) -> str:
    root.mkdir()
    database = root / "conversations.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript("""
            CREATE TABLE conversations (id TEXT PRIMARY KEY, provider TEXT, thread_id TEXT);
            CREATE TABLE turns (id TEXT, conversation_id TEXT, state TEXT, created_at TEXT);
        """)
        db.execute("INSERT INTO conversations VALUES (?,?,?)", ("abc123", "codex", "thread-1"))
        db.execute("INSERT INTO turns VALUES (?,?,?,?)", ("turn-1", "abc123", "running", "2026-11-18"))
    return "abc123"


def test_schedule_persists_request_and_uses_external_systemd_unit(tmp_path, monkeypatch):
    conversation_id = saved_codex_chat(tmp_path / "chat")
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(reset.subprocess, "run", lambda command, **kwargs: calls.append(command) or Result())
    args = argparse.Namespace(data_root=str(tmp_path / "chat"), conversation_id=conversation_id,
                              wait_seconds=30)
    result = reset.schedule(args, {"confirmation": "restart pi-gateway",
        "recovery_note": "Continue the dynamic context allocator tests."})

    assert result["scheduled"] is True
    assert calls and calls[0][:4] == ["sudo", "-n", "systemd-run", "--quiet"]
    assert "--collect" in calls[0]
    receipt = json.loads(next((tmp_path / "chat" / "reset-requests").glob("*.json")).read_text())
    assert receipt["state"] == "scheduled"
    assert receipt["thread_id"] == "thread-1"


def test_schedule_requires_exact_confirmation(tmp_path):
    conversation_id = saved_codex_chat(tmp_path / "chat")
    args = argparse.Namespace(data_root=str(tmp_path / "chat"), conversation_id=conversation_id,
                              wait_seconds=30)
    try:
        reset.schedule(args, {"confirmation": "yes", "recovery_note": "continue"})
    except ValueError as exc:
        assert "confirmation" in str(exc)
    else:
        raise AssertionError("unsafe reset confirmation was accepted")
