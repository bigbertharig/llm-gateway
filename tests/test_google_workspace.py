"""Focused boundary and bounded-content checks for the Google connector."""

import base64

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
