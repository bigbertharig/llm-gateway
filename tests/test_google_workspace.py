"""Focused boundary and bounded-content checks for the Google connector."""

import base64
import json

import pytest

from llm_gateway.dashboard import google_workspace as google
from llm_gateway.manager.errors import ManagerError


def test_disabled_connector_cannot_open_token(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setenv("GOOGLE_WORKSPACE_TOKEN_FILE", str(token))
    monkeypatch.delenv("GOOGLE_WORKSPACE_ENABLED", raising=False)
    with pytest.raises(ManagerError):
        google._settings()


def test_private_token_and_account_are_required(monkeypatch, tmp_path):
    token = tmp_path / "token.json"
    token.write_text("{}")
    token.chmod(0o644)
    monkeypatch.setenv("GOOGLE_WORKSPACE_ENABLED", "1")
    monkeypatch.setenv("GOOGLE_WORKSPACE_ACCOUNT", "bigbertharig@gmail.com")
    monkeypatch.setenv("GOOGLE_WORKSPACE_TOKEN_FILE", str(token))
    with pytest.raises(ManagerError):
        google._settings()
    token.chmod(0o600)
    assert google._settings() == (token, "bigbertharig@gmail.com")


def test_message_body_is_plain_text_and_bounded():
    raw = base64.urlsafe_b64encode(b"a" * (google.MAX_BODY + 20)).decode()
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": "abc"}},
        {"mimeType": "text/plain", "body": {"data": raw}}]}
    assert google._plain_body(payload) == "a" * google.MAX_BODY


def test_write_receipt_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_WORKSPACE_RECEIPT_DIR", str(tmp_path))
    calls = []

    def action():
        calls.append(True)
        return {"message_id": "message-1"}

    first = google._mutate("send_email", "project-update-001",
                           {"to": "test@example.com", "subject": "update"}, action)
    second = google._mutate("send_email", "project-update-001",
                            {"to": "test@example.com", "subject": "update"}, action)
    assert first == second
    assert first["state"] == "completed"
    assert first["result"]["message_id"] == "message-1"
    assert calls == [True]
    assert (tmp_path / "project-update-001.json").stat().st_mode & 0o077 == 0


def test_pending_receipt_fails_closed_without_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_WORKSPACE_RECEIPT_DIR", str(tmp_path))
    (tmp_path / "uncertain-email-001.json").write_text(json.dumps({
        "receipt_id": "uncertain-email-001", "operation": "send_email", "state": "pending"}))
    with pytest.raises(ManagerError) as raised:
        google._mutate("send_email", "uncertain-email-001", {}, lambda: {"message_id": "duplicate"})
    assert raised.value.status == 409


def test_sheet_values_are_bounded():
    assert google._values({"values": [["name", 3, True, None]]}) == [["name", 3, True, None]]
    with pytest.raises(ManagerError):
        google._values({"values": [["x"] * (google.MAX_SHEET_COLUMNS + 1)]})
