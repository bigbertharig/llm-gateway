"""Sanitized dashboard upload inbox and transfer routes."""

import base64
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from llm_gateway.dashboard import transfers
from llm_gateway.dashboard.transfers import TransferStore
from llm_gateway.manager.errors import ManagerError


def data_url(data: bytes, mime: str = "text/plain") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def test_transfer_store_uses_generated_private_path_and_checks_integrity(tmp_path):
    root = tmp_path / "Desktop" / "Transfer"
    store = TransferStore(root)
    content = b"complete report\n" * 900
    saved = store.create({"name": "report.md", "data_url": data_url(content)})
    path = Path(saved["path"])
    assert path.parent == root
    assert path.name == f"{saved['id']}-report.md"
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o600
    assert datetime.fromisoformat(saved["created_at"]).utcoffset().total_seconds() == 0
    assert store.list() == [saved]
    assert store.read(saved["id"])[1] == content

    path.write_bytes(b"changed")
    with pytest.raises(ManagerError, match="integrity"):
        store.read(saved["id"])


def test_transfer_store_rejects_a_corrupt_index_path(tmp_path):
    store = TransferStore(tmp_path / "Transfer")
    saved = store.create({"name": "report.md", "data_url": data_url(b"report")})
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE transfers SET storage_name='../escape' WHERE id=?", (saved["id"],))
    with pytest.raises(ManagerError, match="index is invalid"):
        store.read(saved["id"])
    with pytest.raises(ManagerError, match="index is invalid"):
        store.list()


@pytest.mark.parametrize("item", [
    {"name": "../escape.md", "data_url": data_url(b"safe")},
    {"name": "run.exe", "data_url": data_url(b"MZ")},
    {"name": "binary.md", "data_url": data_url(b"text\x00binary")},
    {"name": "fake.pdf", "data_url": data_url(b"not a pdf", "application/pdf")},
])
def test_transfer_store_rejects_unsafe_or_mismatched_files(tmp_path, item):
    with pytest.raises(ManagerError):
        TransferStore(tmp_path / "Transfer").create(item)


def test_transfer_api_lists_downloads_and_deletes_admitted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRANSFER_ROOT", str(tmp_path / "Transfer"))
    app = FastAPI()
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    app.include_router(transfers.router)
    with TestClient(app) as client:
        uploaded = client.post("/dashboard/api/transfers", json={
            "name": "notes.md", "data_url": data_url(b"all notes")})
        assert uploaded.status_code == 200
        record = uploaded.json()
        assert record["path"].startswith(str(tmp_path / "Transfer"))
        listed = client.get("/dashboard/api/transfers").json()["transfers"]
        assert listed == [record]
        downloaded = client.get("/dashboard/api/transfers/" + record["id"])
        assert downloaded.content == b"all notes"
        assert "notes.md" in downloaded.headers["content-disposition"]
        assert client.delete("/dashboard/api/transfers/" + record["id"]).status_code == 200
        assert not Path(record["path"]).exists()
        assert client.get("/dashboard/api/transfers/" + record["id"]).status_code == 404
