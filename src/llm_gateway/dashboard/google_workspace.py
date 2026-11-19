"""Gateway-owned, read-only Google connector for the dedicated rig account.

The harness calls typed endpoints and never receives OAuth credentials. This
module is disabled until a private token is created and explicitly enabled.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request

from ..manager.errors import ManagerError

router = APIRouter(prefix="/dashboard/api/google", tags=["google-workspace"])
OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
MAX_BODY = 16000


def _settings() -> tuple[Path, str]:
    if os.getenv("GOOGLE_WORKSPACE_ENABLED") != "1":
        raise ManagerError(503, "Google Workspace is not connected")
    token = Path(os.getenv("GOOGLE_WORKSPACE_TOKEN_FILE", "")).expanduser()
    account = os.getenv("GOOGLE_WORKSPACE_ACCOUNT", "").strip().lower()
    if not token.is_absolute() or not account or token.is_symlink() or not token.is_file():
        raise ManagerError(503, "Google Workspace credentials are unavailable")
    if token.stat().st_mode & 0o077:
        raise ManagerError(503, "Google Workspace token permissions are too broad")
    return token, account


def _service(api: str, version: str):
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ManagerError(503, "Google Workspace dependencies are unavailable") from exc
    token, account = _settings()
    scopes = ["https://www.googleapis.com/auth/gmail.readonly",
              "https://www.googleapis.com/auth/drive.readonly"]
    try:
        creds = Credentials.from_authorized_user_file(str(token), scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=token.parent,
                                             prefix=".google-token-", delete=False) as tmp:
                tmp.write(creds.to_json())
                tmp.flush()
                os.fchmod(tmp.fileno(), 0o600)
                staged = Path(tmp.name)
            os.replace(staged, token)
        if not creds.valid:
            raise ManagerError(503, "Google Workspace authorization expired; reconnect the account")
        service = build(api, version, credentials=creds, cache_discovery=False)
        if api == "gmail":
            actual = service.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
        else:
            actual = service.about().get(fields="user(emailAddress)").execute().get("user", {}).get("emailAddress", "").lower()
        if actual != account:
            raise ManagerError(503, "Google Workspace account does not match the configured rig account")
        return service
    except ManagerError:
        raise
    except Exception as exc:
        raise ManagerError(503, "Google Workspace is unavailable; check authorization") from exc


async def _body(request: Request, field: str, *, minimum=1, maximum=200) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ManagerError(400, "Invalid Google tool request") from exc
    if not isinstance(body, dict) or set(body) - {field, "limit"}:
        raise ManagerError(400, "Invalid Google tool arguments")
    value = body.get(field)
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ManagerError(400, "Invalid Google query or object ID")
    limit = body.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ManagerError(400, "Google result limit must be 1 to 20")
    if field.endswith("_id") and not OBJECT_ID.fullmatch(value):
        raise ManagerError(400, "Invalid Google object ID")
    return {field: value, "limit": limit}


def _headers(message: dict) -> dict:
    return {h.get("name", "").lower(): h.get("value", "")[:500]
            for h in message.get("payload", {}).get("headers", []) if isinstance(h, dict)}


def _plain_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and isinstance(payload.get("body", {}).get("data"), str):
        raw = payload["body"]["data"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")[:MAX_BODY]
    for part in payload.get("parts", [])[:30]:
        if isinstance(part, dict):
            found = _plain_body(part)
            if found:
                return found
    return ""


def _search_mail(query: str, limit: int):
    service = _service("gmail", "v1")
    rows = service.users().messages().list(userId="me", q=query, maxResults=limit).execute().get("messages", [])
    return {"messages": [{"id": r.get("id"), "thread_id": r.get("threadId")}
                         for r in rows[:limit] if isinstance(r, dict)],
            "source": "gmail", "content_trust": "untrusted"}


def _read_mail(message_id: str):
    service = _service("gmail", "v1")
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = _headers(message)
    body = _plain_body(message.get("payload", {}))
    return {"id": message.get("id"), "thread_id": message.get("threadId"),
            "from": headers.get("from"), "to": headers.get("to"),
            "subject": headers.get("subject"), "date": headers.get("date"),
            "body": body, "body_truncated": len(body) >= MAX_BODY,
            "source": "gmail", "content_trust": "untrusted"}


def _search_drive(query: str, limit: int):
    service = _service("drive", "v3")
    escaped = query.replace("\\", "\\\\").replace("'", "\\'")
    rows = service.files().list(q=f"name contains '{escaped}' and trashed = false",
                                page_size=limit, fields="files(id,name,mimeType,size,modifiedTime,webViewLink)").execute().get("files", [])
    return {"files": [{key: row.get(key) for key in ("id", "name", "mimeType", "size", "modifiedTime", "webViewLink")}
                      for row in rows[:limit] if isinstance(row, dict)],
            "source": "drive", "content_trust": "untrusted"}


def _read_drive_text(file_id: str):
    service = _service("drive", "v3")
    metadata = service.files().get(fileId=file_id, fields="id,name,mimeType,size,webViewLink").execute()
    mime = metadata.get("mimeType")
    if mime == "application/vnd.google-apps.document":
        raw = service.files().export(fileId=file_id, mimeType="text/plain").execute()
    elif mime in ("text/plain", "text/markdown", "text/csv", "application/json"):
        raw = service.files().get_media(fileId=file_id).execute()
    else:
        raise ManagerError(415, "Drive file is not a supported text document")
    if not isinstance(raw, bytes):
        raise ManagerError(502, "Google Drive returned an invalid document")
    return {"id": metadata.get("id"), "name": metadata.get("name"), "mime_type": mime,
            "text": raw[:MAX_BODY].decode("utf-8", "replace"), "truncated": len(raw) > MAX_BODY,
            "source": "drive", "content_trust": "untrusted"}


@router.post("/mail/search")
async def search_mail(request: Request):
    args = await _body(request, "query")
    return await asyncio.to_thread(_search_mail, args["query"], args["limit"])


@router.post("/mail/read")
async def read_mail(request: Request):
    args = await _body(request, "message_id", minimum=4, maximum=128)
    return await asyncio.to_thread(_read_mail, args["message_id"])


@router.post("/drive/search")
async def search_drive(request: Request):
    args = await _body(request, "query")
    return await asyncio.to_thread(_search_drive, args["query"], args["limit"])


@router.post("/drive/read-text")
async def read_drive_text(request: Request):
    args = await _body(request, "file_id", minimum=4, maximum=128)
    return await asyncio.to_thread(_read_drive_text, args["file_id"])
