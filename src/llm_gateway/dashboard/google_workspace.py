"""Gateway-owned Google Workspace connector for the dedicated rig account.

OAuth credentials stay in Gateway. Model-facing MCP processes receive only
typed results. Mutations require explicit confirmation and an idempotency key;
Gateway writes a durable pending/completed receipt around every external call.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Request

from ..orchestration_client import ManagerError

router = APIRouter(prefix="/dashboard/api/google", tags=["google-workspace"])
OBJECT_ID = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
MAX_BODY = 16000
MAX_WRITE_TEXT = 50000
MAX_SHEET_ROWS = 100
MAX_SHEET_COLUMNS = 30
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
TEXT_MIME_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}
IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 64 * 1024 * 1024


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
    try:
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
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
        if api == "gmail":
            service = build(api, version, credentials=creds, cache_discovery=False)
            actual = service.users().getProfile(userId="me").execute().get("emailAddress", "").lower()
        else:
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
            actual = drive.about().get(fields="user(emailAddress)").execute().get(
                "user", {}).get("emailAddress", "").lower()
            service = drive if api == "drive" else build(
                api, version, credentials=creds, cache_discovery=False)
        if actual != account:
            raise ManagerError(503, "Google Workspace account does not match the configured rig account")
        return service
    except ManagerError:
        raise
    except Exception as exc:
        raise ManagerError(503, "Google Workspace is unavailable; check authorization") from exc


async def _json_body(request: Request, allowed: set[str], required: set[str]) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise ManagerError(400, "Invalid Google tool request") from exc
    if not isinstance(body, dict) or set(body) - allowed or not required <= set(body):
        raise ManagerError(400, "Invalid Google tool arguments")
    return body


async def _body(request: Request, field: str, *, minimum=1, maximum=200) -> dict:
    body = await _json_body(request, {field, "limit"}, {field})
    value = body[field]
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ManagerError(400, "Invalid Google query or object ID")
    limit = body.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ManagerError(400, "Google result limit must be 1 to 20")
    if field.endswith("_id") and not OBJECT_ID.fullmatch(value):
        raise ManagerError(400, "Invalid Google object ID")
    return {field: value, "limit": limit}


def _text(body: dict, field: str, minimum: int, maximum: int) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ManagerError(400, f"Invalid Google {field}")
    return value


def _object_id(body: dict, field: str, *, optional: bool = False) -> str | None:
    value = body.get(field)
    if optional and value is None:
        return None
    if not isinstance(value, str) or not OBJECT_ID.fullmatch(value):
        raise ManagerError(400, f"Invalid Google {field}")
    return value


def _mutation_fields(body: dict) -> str:
    request_id = body.get("request_id")
    if body.get("confirmed") is not True:
        raise ManagerError(400, "Google write operation requires confirmed=true")
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise ManagerError(400, "Google write operation requires a valid request_id")
    return request_id


def _receipt_dir() -> Path:
    directory = Path(os.getenv("GOOGLE_WORKSPACE_RECEIPT_DIR") or
                     (Path.home() / ".local/share/llm-gateway/google-workspace-receipts"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _write_json(path: Path, payload: dict) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".google-receipt-", delete=False) as tmp:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
        tmp.flush()
        os.fchmod(tmp.fileno(), 0o600)
        staged = Path(tmp.name)
    os.replace(staged, path)


def _mutate(operation: str, request_id: str, target: dict,
            action: Callable[[], dict]) -> dict:
    directory = _receipt_dir()
    receipt_path = directory / f"{request_id}.json"
    lock_path = directory / ".lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ManagerError(503, "Google operation receipt is unreadable") from exc
            if receipt.get("operation") != operation:
                raise ManagerError(409, "Google request_id was already used for another operation")
            if receipt.get("state") != "completed":
                raise ManagerError(409, "Google operation outcome is uncertain; inspect its receipt")
            return receipt
        receipt = {
            "receipt_id": request_id,
            "operation": operation,
            "state": "pending",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "target": target,
        }
        _write_json(receipt_path, receipt)
        result = action()
        if not isinstance(result, dict):
            raise ManagerError(502, "Google returned an invalid mutation result")
        receipt.update(state="completed", completed_at=datetime.now(timezone.utc).isoformat(),
                       result=result)
        _write_json(receipt_path, receipt)
        return receipt


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


def _send_email(recipient: str, subject: str, text_body: str):
    service = _service("gmail", "v1")
    _, account = _settings()
    message = EmailMessage()
    message["From"] = account
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent.get("id"), "thread_id": sent.get("threadId"),
            "recipient": recipient, "subject": subject}


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
    elif mime in TEXT_MIME_TYPES:
        raw = service.files().get_media(fileId=file_id).execute()
    else:
        raise ManagerError(415, "Drive file is not a supported text document")
    if not isinstance(raw, bytes):
        raise ManagerError(502, "Google Drive returned an invalid document")
    return {"id": metadata.get("id"), "name": metadata.get("name"), "mime_type": mime,
            "text": raw[:MAX_BODY].decode("utf-8", "replace"), "truncated": len(raw) > MAX_BODY,
            "source": "drive", "content_trust": "untrusted"}


def _file_result(file_id: str) -> dict:
    service = _service("drive", "v3")
    row = service.files().get(fileId=file_id, fields="id,name,mimeType,parents,webViewLink").execute()
    return {key: row.get(key) for key in ("id", "name", "mimeType", "parents", "webViewLink")}


def _place_file(file_id: str, folder_id: str | None) -> None:
    if not folder_id:
        return
    drive = _service("drive", "v3")
    parents = drive.files().get(fileId=file_id, fields="parents").execute().get("parents", [])
    drive.files().update(fileId=file_id, addParents=folder_id,
                         removeParents=",".join(parents), fields="id,parents").execute()


def _create_folder(name: str, parent_id: str | None):
    drive = _service("drive", "v3")
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    row = drive.files().create(body=metadata, fields="id,name,mimeType,parents,webViewLink").execute()
    return {key: row.get(key) for key in ("id", "name", "mimeType", "parents", "webViewLink")}


def _upload_text(name: str, content: str, mime_type: str, folder_id: str | None):
    try:
        from googleapiclient.http import MediaInMemoryUpload
    except ImportError as exc:
        raise ManagerError(503, "Google Workspace dependencies are unavailable") from exc
    drive = _service("drive", "v3")
    metadata = {"name": name}
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype=mime_type, resumable=False)
    row = drive.files().create(body=metadata, media_body=media,
                               fields="id,name,mimeType,parents,webViewLink,size").execute()
    return {key: row.get(key) for key in ("id", "name", "mimeType", "parents", "webViewLink", "size")}


def _allowed_image(path: str, mime_type: str) -> Path:
    """Only expose images under explicitly configured export roots to model tools."""
    roots = os.getenv("GOOGLE_WORKSPACE_UPLOAD_ROOTS", "").split(os.pathsep)
    allowed_roots = [Path(root).expanduser().resolve() for root in roots if root]
    if not allowed_roots:
        raise ManagerError(503, "Google Drive image upload roots are not configured")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise ManagerError(400, "Google Drive image path must be an absolute regular file")
    try:
        resolved = candidate.resolve(strict=True)
        size = resolved.stat().st_size
    except (OSError, ValueError) as exc:
        raise ManagerError(400, "Google Drive image is unavailable") from exc
    if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise ManagerError(403, "Google Drive image is outside configured upload roots")
    if mime_type not in IMAGE_MIME_TYPES or resolved.suffix.lower() not in {
        "image/png": {".png"}, "image/jpeg": {".jpg", ".jpeg"}, "image/webp": {".webp"}
    }[mime_type]:
        raise ManagerError(400, "Unsupported Google Drive image type")
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ManagerError(400, "Google Drive image exceeds the upload size limit")
    return resolved


def _upload_image(path: Path, name: str, mime_type: str, folder_id: str | None) -> dict:
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise ManagerError(503, "Google Workspace dependencies are unavailable") from exc
    drive = _service("drive", "v3")
    metadata = {"name": name}
    if folder_id:
        metadata["parents"] = [folder_id]
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    row = drive.files().create(body=metadata, media_body=media,
                               fields="id,name,mimeType,parents,webViewLink,size").execute()
    return {key: row.get(key) for key in ("id", "name", "mimeType", "parents", "webViewLink", "size")}


def _create_document(title: str, initial_text: str, folder_id: str | None):
    docs = _service("docs", "v1")
    document = docs.documents().create(body={"title": title}).execute()
    document_id = document.get("documentId")
    if initial_text:
        docs.documents().batchUpdate(documentId=document_id, body={"requests": [
            {"insertText": {"location": {"index": 1}, "text": initial_text}}
        ]}).execute()
    _place_file(document_id, folder_id)
    return _file_result(document_id)


def _append_document(document_id: str, text: str):
    docs = _service("docs", "v1")
    reply = docs.documents().batchUpdate(documentId=document_id, body={"requests": [
        {"insertText": {"endOfSegmentLocation": {}, "text": text}}
    ]}).execute()
    return {"document_id": document_id, "write_control": reply.get("writeControl")}


def _create_sheet(title: str, sheet_title: str | None, folder_id: str | None):
    sheets = _service("sheets", "v4")
    body = {"properties": {"title": title}}
    if sheet_title:
        body["sheets"] = [{"properties": {"title": sheet_title}}]
    result = sheets.spreadsheets().create(body=body, fields="spreadsheetId").execute()
    spreadsheet_id = result.get("spreadsheetId")
    _place_file(spreadsheet_id, folder_id)
    return _file_result(spreadsheet_id)


def _read_sheet(spreadsheet_id: str, cell_range: str):
    result = _service("sheets", "v4").spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=cell_range).execute()
    values = result.get("values", [])
    return {"spreadsheet_id": spreadsheet_id, "range": result.get("range"),
            "values": values[:MAX_SHEET_ROWS], "truncated": len(values) > MAX_SHEET_ROWS,
            "source": "sheets", "content_trust": "untrusted"}


def _write_sheet(spreadsheet_id: str, cell_range: str, values: list[list], *, append: bool):
    resource = _service("sheets", "v4").spreadsheets().values()
    if append:
        result = resource.append(spreadsheetId=spreadsheet_id, range=cell_range,
                                 valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
                                 body={"values": values}).execute()
        updates = result.get("updates", {})
    else:
        result = resource.update(spreadsheetId=spreadsheet_id, range=cell_range,
                                 valueInputOption="USER_ENTERED", body={"values": values}).execute()
        updates = result
    return {"spreadsheet_id": spreadsheet_id, "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows"), "updated_columns": updates.get("updatedColumns"),
            "updated_cells": updates.get("updatedCells")}


def _values(body: dict) -> list[list]:
    values = body.get("values")
    scalar = (str, int, float, bool)
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_SHEET_ROWS or
            any(not isinstance(row, list) or not 1 <= len(row) <= MAX_SHEET_COLUMNS
                or any(value is not None and not isinstance(value, scalar) for value in row)
                for row in values)):
        raise ManagerError(400, "Invalid Google Sheets values")
    return values


@router.post("/mail/search")
async def search_mail(request: Request):
    args = await _body(request, "query")
    return await asyncio.to_thread(_search_mail, args["query"], args["limit"])


@router.post("/mail/read")
async def read_mail(request: Request):
    args = await _body(request, "message_id", minimum=4, maximum=128)
    return await asyncio.to_thread(_read_mail, args["message_id"])


@router.post("/mail/send")
async def send_email(request: Request):
    body = await _json_body(request, {"to", "subject", "body", "request_id", "confirmed"},
                            {"to", "subject", "body", "request_id", "confirmed"})
    recipient = _text(body, "to", 3, 320)
    _, parsed = parseaddr(recipient)
    if parsed != recipient or "\n" in recipient or "\r" in recipient or "@" not in recipient:
        raise ManagerError(400, "Invalid Google email recipient")
    subject = _text(body, "subject", 1, 200)
    text_body = _text(body, "body", 1, MAX_WRITE_TEXT)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "send_email", request_id,
                                   {"to": recipient, "subject": subject},
                                   lambda: _send_email(recipient, subject, text_body))


@router.post("/drive/search")
async def search_drive(request: Request):
    args = await _body(request, "query")
    return await asyncio.to_thread(_search_drive, args["query"], args["limit"])


@router.post("/drive/read-text")
async def read_drive_text(request: Request):
    args = await _body(request, "file_id", minimum=4, maximum=128)
    return await asyncio.to_thread(_read_drive_text, args["file_id"])


@router.post("/drive/create-folder")
async def create_drive_folder(request: Request):
    body = await _json_body(request, {"name", "parent_id", "request_id", "confirmed"},
                            {"name", "request_id", "confirmed"})
    name = _text(body, "name", 1, 255)
    parent_id = _object_id(body, "parent_id", optional=True)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "create_drive_folder", request_id,
                                   {"name": name, "parent_id": parent_id},
                                   lambda: _create_folder(name, parent_id))


@router.post("/drive/upload-text")
async def upload_drive_text(request: Request):
    body = await _json_body(request, {"name", "content", "mime_type", "folder_id", "request_id", "confirmed"},
                            {"name", "content", "mime_type", "request_id", "confirmed"})
    name = _text(body, "name", 1, 255)
    content = _text(body, "content", 1, MAX_WRITE_TEXT)
    mime_type = body.get("mime_type")
    if mime_type not in TEXT_MIME_TYPES:
        raise ManagerError(400, "Unsupported Google Drive text MIME type")
    folder_id = _object_id(body, "folder_id", optional=True)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "upload_drive_text", request_id,
                                   {"name": name, "mime_type": mime_type, "folder_id": folder_id},
                                   lambda: _upload_text(name, content, mime_type, folder_id))


@router.post("/drive/upload-image")
async def upload_drive_image(request: Request):
    body = await _json_body(request, {"path", "name", "mime_type", "folder_id", "request_id", "confirmed"},
                            {"path", "name", "mime_type", "request_id", "confirmed"})
    path = _text(body, "path", 1, 4096)
    name = _text(body, "name", 1, 255)
    mime_type = _text(body, "mime_type", 1, 80)
    image = _allowed_image(path, mime_type)
    folder_id = _object_id(body, "folder_id", optional=True)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "upload_drive_image", request_id,
                                   {"path": str(image), "name": name, "mime_type": mime_type,
                                    "folder_id": folder_id, "size": image.stat().st_size},
                                   lambda: _upload_image(image, name, mime_type, folder_id))


@router.post("/docs/create")
async def create_document(request: Request):
    body = await _json_body(request, {"title", "initial_text", "folder_id", "request_id", "confirmed"},
                            {"title", "request_id", "confirmed"})
    title = _text(body, "title", 1, 255)
    initial_text = body.get("initial_text", "")
    if not isinstance(initial_text, str) or len(initial_text) > MAX_WRITE_TEXT:
        raise ManagerError(400, "Invalid Google initial_text")
    folder_id = _object_id(body, "folder_id", optional=True)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "create_google_doc", request_id,
                                   {"title": title, "folder_id": folder_id},
                                   lambda: _create_document(title, initial_text, folder_id))


@router.post("/docs/append")
async def append_document(request: Request):
    body = await _json_body(request, {"document_id", "text", "request_id", "confirmed"},
                            {"document_id", "text", "request_id", "confirmed"})
    document_id = _object_id(body, "document_id")
    text = _text(body, "text", 1, MAX_WRITE_TEXT)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "append_google_doc_text", request_id,
                                   {"document_id": document_id},
                                   lambda: _append_document(document_id, text))


@router.post("/sheets/create")
async def create_sheet(request: Request):
    body = await _json_body(request, {"title", "sheet_title", "folder_id", "request_id", "confirmed"},
                            {"title", "request_id", "confirmed"})
    title = _text(body, "title", 1, 255)
    sheet_title = body.get("sheet_title")
    if sheet_title is not None and (not isinstance(sheet_title, str) or not 1 <= len(sheet_title) <= 100):
        raise ManagerError(400, "Invalid Google sheet_title")
    folder_id = _object_id(body, "folder_id", optional=True)
    request_id = _mutation_fields(body)
    return await asyncio.to_thread(_mutate, "create_google_sheet", request_id,
                                   {"title": title, "sheet_title": sheet_title, "folder_id": folder_id},
                                   lambda: _create_sheet(title, sheet_title, folder_id))


@router.post("/sheets/read")
async def read_sheet(request: Request):
    body = await _json_body(request, {"spreadsheet_id", "range"}, {"spreadsheet_id", "range"})
    spreadsheet_id = _object_id(body, "spreadsheet_id")
    cell_range = _text(body, "range", 1, 200)
    return await asyncio.to_thread(_read_sheet, spreadsheet_id, cell_range)


async def _sheet_write(request: Request, *, append: bool):
    body = await _json_body(request, {"spreadsheet_id", "range", "values", "request_id", "confirmed"},
                            {"spreadsheet_id", "range", "values", "request_id", "confirmed"})
    spreadsheet_id = _object_id(body, "spreadsheet_id")
    cell_range = _text(body, "range", 1, 200)
    values = _values(body)
    request_id = _mutation_fields(body)
    operation = "append_google_sheet_rows" if append else "update_google_sheet"
    return await asyncio.to_thread(_mutate, operation, request_id,
                                   {"spreadsheet_id": spreadsheet_id, "range": cell_range,
                                    "rows": len(values), "columns": max(map(len, values))},
                                   lambda: _write_sheet(spreadsheet_id, cell_range, values, append=append))


@router.post("/sheets/update")
async def update_sheet(request: Request):
    return await _sheet_write(request, append=False)


@router.post("/sheets/append")
async def append_sheet(request: Request):
    return await _sheet_write(request, append=True)
