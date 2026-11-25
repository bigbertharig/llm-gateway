"""Sanitized, operator-owned file transfers into the Gateway host."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..orchestration_client import ManagerError

router = APIRouter(prefix="/dashboard/api/transfers", tags=["dashboard-transfers"])

MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 3
DATA_URL = re.compile(r"^data:([a-zA-Z0-9.+/-]+);base64,([A-Za-z0-9+/=]+)$")
TRANSFER_ID = re.compile(r"^[0-9a-f]{32}$")
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".html", ".xml", ".py", ".js", ".ts", ".css", ".log"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root() -> Path:
    configured = os.getenv("DASHBOARD_TRANSFER_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Desktop" / "Transfer"


def _simple_name(value: object) -> str:
    if not isinstance(value, str):
        raise ManagerError(400, "transfer needs a file name")
    name = unicodedata.normalize("NFC", value.strip())
    if (not name or len(name) > 160 or Path(name).name != name
            or any(unicodedata.category(char).startswith("C") for char in name)):
        raise ManagerError(400, "transfer needs a simple file name")
    if Path(name).suffix.lower() not in TEXT_SUFFIXES | {".pdf"}:
        raise ManagerError(400, "unsupported transfer file type")
    return name


def _admit(item: object) -> tuple[str, str, bytes]:
    if not isinstance(item, dict):
        raise ManagerError(400, "transfer must be an object")
    name = _simple_name(item.get("name"))
    value = item.get("data_url")
    match = DATA_URL.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise ManagerError(400, "transfer needs a base64 data URL")
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ManagerError(400, "transfer contains invalid base64 data") from exc
    if not data or len(data) > MAX_BYTES:
        raise ManagerError(413, "transfer must be between 1 byte and 8 MiB")
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise ManagerError(400, "file is not a PDF")
        mime = "application/pdf"
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ManagerError(400, "text transfer must be UTF-8") from exc
        if "\x00" in text:
            raise ManagerError(400, "text transfer contains binary data")
        mime = {".json": "application/json", ".xml": "application/xml",
                ".js": "application/javascript"}.get(suffix, "text/plain")
    return name, mime, data


class TransferStore:
    def __init__(self, root: Path | None = None):
        requested = root or _root()
        if requested.is_symlink():
            raise RuntimeError("transfer root cannot be a symlink")
        self.root = requested.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.database = self.root / ".index.sqlite3"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS transfers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mime_type TEXT NOT NULL,
                storage_name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            )""")
        os.chmod(self.database, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, item: object) -> dict:
        name, mime, data = _admit(item)
        transfer_id = uuid.uuid4().hex
        storage_name = transfer_id + "-" + name
        path = self.root / storage_name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            record = {"id": transfer_id, "name": name, "type": mime,
                      "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                      "created_at": _now(), "path": str(path)}
            with self._db() as db:
                db.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?)", (
                    record["id"], record["name"], record["type"], storage_name, record["size_bytes"],
                    record["sha256"], record["created_at"]))
            return record
        except Exception:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            raise

    def _path_for(self, row: sqlite3.Row) -> Path:
        expected = f"{row['id']}-{row['name']}"
        if (not TRANSFER_ID.fullmatch(row["id"]) or Path(expected).name != expected
                or row["storage_name"] != expected):
            raise ManagerError(409, "transfer index is invalid")
        return self.root / expected

    def list(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM transfers ORDER BY created_at DESC,id DESC").fetchall()
        return [{"id": row["id"], "name": row["name"], "type": row["mime_type"],
                 "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                 "created_at": row["created_at"], "path": str(self._path_for(row))} for row in rows]

    def read(self, transfer_id: str) -> tuple[dict, bytes]:
        if not isinstance(transfer_id, str) or not TRANSFER_ID.fullmatch(transfer_id):
            raise ManagerError(404, "transfer not found")
        with self._db() as db:
            row = db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise ManagerError(404, "transfer not found")
        try:
            fd = os.open(self._path_for(row), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                data = source.read(MAX_BYTES + 1)
        except OSError as exc:
            raise ManagerError(409, "transferred file is unavailable") from exc
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ManagerError(409, "transferred file failed its integrity check")
        record = {"id": row["id"], "name": row["name"], "type": row["mime_type"],
                  "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                  "created_at": row["created_at"], "path": str(self._path_for(row))}
        return record, data

    def delete(self, transfer_id: str) -> None:
        record, _ = self.read(transfer_id)
        with self._db() as db:
            if db.execute("DELETE FROM transfers WHERE id=?", (transfer_id,)).rowcount != 1:
                raise ManagerError(404, "transfer not found")
        try:
            Path(record["path"]).unlink()
        except FileNotFoundError:
            pass


def store() -> TransferStore:
    return TransferStore()


def selected_items(value: object) -> list[dict]:
    if value is None:
        return []
    if (not isinstance(value, list) or len(value) > MAX_ITEMS
            or any(not isinstance(item, str) or not TRANSFER_ID.fullmatch(item) for item in value)):
        raise ManagerError(400, f"transfers must contain at most {MAX_ITEMS} transfer IDs")
    if len(set(value)) != len(value):
        raise ManagerError(400, "transfers must not contain duplicates")
    result = []
    transfer_store = store()
    for transfer_id in value:
        record, data = transfer_store.read(transfer_id)
        result.append({"name": record["name"], "type": record["type"],
                       "data_url": f"data:{record['type']};base64,{base64.b64encode(data).decode()}",
                       "transfer_id": transfer_id})
    return result


@router.get("")
async def list_transfers():
    return {"transfers": store().list(), "max_bytes": MAX_BYTES}


@router.post("")
async def create_transfer(request: Request):
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManagerError(400, "invalid JSON body") from exc
    return store().create(body)


@router.get("/{transfer_id}")
async def download_transfer(transfer_id: str):
    record, data = store().read(transfer_id)
    encoded = quote(record["name"], safe="")
    return Response(data, media_type=record["type"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.delete("/{transfer_id}")
async def delete_transfer(transfer_id: str):
    store().delete(transfer_id)
    return {"deleted": transfer_id}
