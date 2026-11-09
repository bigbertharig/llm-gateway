"""Persistent dashboard chat, using published gateway profiles and the rig manager."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from ..manager.core import Manager
from ..manager.errors import ManagerError
from ..rig import RigUnavailable
from ..profiles import Profiles

MAX_MESSAGE = 8000
HISTORY_MESSAGES = 16


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChatStore:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.path = root / "conversations.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                    content TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id, id);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
            for name in ("context_length", "last_prompt_tokens", "last_completion_tokens", "last_total_tokens"):
                if name not in columns:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {name} INTEGER")
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list(self) -> list[dict]:
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT 200")]

    def create(self, model: str) -> dict:
        now, cid = _now(), uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO conversations (id,title,model,created_at,updated_at) VALUES (?,?,?,?,?)", (cid, "New conversation", model, now, now))
        return self.get(cid)

    def get(self, cid: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise ManagerError(404, "conversation not found")
            messages = [dict(r) for r in db.execute(
                "SELECT id, role, content, model, created_at FROM messages WHERE conversation_id=? ORDER BY id", (cid,))]
        return {**dict(row), "messages": messages}

    def switch_model(self, cid: str, expected_model: str, new_model: str) -> dict:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT model FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise ManagerError(404, "conversation not found")
            if row["model"] != expected_model:
                raise ManagerError(409, "conversation model changed in another tab; refresh before switching")
            db.execute(
                "UPDATE conversations SET model=?,updated_at=?,context_length=NULL,"
                "last_prompt_tokens=NULL,last_completion_tokens=NULL,last_total_tokens=NULL WHERE id=?",
                (new_model, _now(), cid))
        return self.get(cid)

    def delete(self, cid: str) -> None:
        with self._db() as db:
            if db.execute("DELETE FROM conversations WHERE id=?", (cid,)).rowcount == 0:
                raise ManagerError(404, "conversation not found")

    def append_exchange(self, cid: str, expected_count: int, model: str, user: str, answer: str,
                        context_length: int | None = None, prompt_tokens: int | None = None,
                        completion_tokens: int | None = None, total_tokens: int | None = None) -> dict:
        now = _now()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT title,model FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise ManagerError(404, "conversation not found")
            if row["model"] != model:
                raise ManagerError(409, "conversation model changed while the turn was running; refresh before sending")
            count = db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
            if count != expected_count:
                raise ManagerError(409, "conversation changed in another tab; refresh before sending")
            db.executemany("INSERT INTO messages (conversation_id,role,content,model,created_at) VALUES (?,?,?,?,?)",
                           [(cid, "user", user, model, now), (cid, "assistant", answer, model, now)])
            title = user.strip().replace("\n", " ")[:64] if count == 0 else row["title"]
            db.execute(
                "UPDATE conversations SET title=?,model=?,updated_at=?,context_length=?,"
                "last_prompt_tokens=?,last_completion_tokens=?,last_total_tokens=? WHERE id=?",
                (title, model, now, context_length, prompt_tokens, completion_tokens, total_tokens, cid))
        return self.get(cid)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManagerError(400, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise ManagerError(400, "body must be an object")
    return body


def router(manager: Manager, profiles: Profiles, forward_inference) -> APIRouter:
    root = Path(os.getenv("CHAT_DATA_ROOT") or (Path.home() / ".local/share/llm-gateway/chat"))
    store: ChatStore | None = None
    init_lock = threading.Lock()

    def get_store() -> ChatStore:
        nonlocal store
        if store is None:
            with init_lock:
                if store is None:
                    store = ChatStore(root)
        return store

    r = APIRouter(prefix="/dashboard/api/chat")

    def offered() -> dict[str, dict]:
        published = profiles.all()
        return {pid: spec for pid, spec in published.items()
                if "chatbot" in spec.get("good_for", [])
                and spec.get("status") == "active" and spec.get("use") == "chat"}

    @r.get("/options")
    async def options():
        try:
            availability = await manager.chat_availability()
            rig_available = True
        except (ManagerError, RigUnavailable):
            availability, rig_available = {"busy": True, "reason": "rig unavailable", "ready_profiles": []}, False
        ready = set(availability["ready_profiles"])
        return {"rig_available": rig_available, "rig_busy": availability["busy"],
                "busy_reason": availability["reason"],
                "models": [{"id": pid, "model": p["model"], "description": p.get("description", ""),
                            "ready": pid in ready} for pid, p in offered().items()]}

    @r.get("/conversations")
    async def conversations():
        return {"conversations": await asyncio.to_thread(get_store().list)}

    @r.post("/conversations", status_code=201)
    async def create(request: Request):
        body = await _json_body(request)
        model = body.get("model")
        if model not in offered():
            raise ManagerError(400, "choose a listed chat model")
        return await asyncio.to_thread(get_store().create, model)

    @r.get("/conversations/{cid}")
    async def get(cid: str):
        return await asyncio.to_thread(get_store().get, cid)

    @r.delete("/conversations/{cid}")
    async def delete(cid: str):
        await asyncio.to_thread(get_store().delete, cid)
        return {"deleted": cid}

    @r.post("/conversations/{cid}/model")
    async def switch_model(cid: str, request: Request):
        body = await _json_body(request)
        model, expected = body.get("model"), body.get("expected_model")
        if model not in offered() or not isinstance(expected, str):
            raise ManagerError(400, "choose a listed chat model and provide expected_model")
        return await asyncio.to_thread(get_store().switch_model, cid, expected, model)

    @r.post("/conversations/{cid}/prepare")
    async def prepare(cid: str):
        conversation = await asyncio.to_thread(get_store().get, cid)
        model = conversation["model"]
        spec = offered().get(model)
        if spec is None:
            raise ManagerError(409, "conversation model is no longer offered")
        prepared = await asyncio.wait_for(manager.prepare_chat_profile(model, spec), timeout=900)
        return {"profile": model, "state": prepared["state"], "context_length": prepared.get("context_length"),
                "gpu_ids": prepared.get("gpu_ids", [])}

    @r.post("/conversations/{cid}/turn")
    async def turn(cid: str, request: Request):
        body = await _json_body(request)
        model, prompt = body.get("model"), body.get("message")
        choices = offered()
        if model not in choices:
            raise ManagerError(400, "choose a listed chat model")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_MESSAGE:
            raise ManagerError(400, f"message must be 1 to {MAX_MESSAGE} characters")
        conversation = await asyncio.to_thread(get_store().get, cid)
        if model != conversation["model"]:
            raise ManagerError(409, "conversation uses another model; switch it explicitly or start a new conversation")
        spec = choices[model]
        prepared = await asyncio.wait_for(manager.prepare_chat_profile(model, spec), timeout=900)
        port = prepared["port"]
        history = conversation["messages"][-HISTORY_MESSAGES:]
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": prompt.strip()})
        payload = json.dumps({"model": f"gpu-rig/{model}", "messages": messages,
                              "stream": False, "max_tokens": 768}).encode()
        response = await forward_inference(request, "/v1/chat/completions", payload, port, False, model)
        if response.status_code != 200:
            return response
        try:
            result = json.loads(response.body)
            answer = result["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ManagerError(502, "model returned no chat message") from exc
        if not isinstance(answer, str) or not answer.strip():
            raise ManagerError(502, "model returned an empty chat message")
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        def token_count(name: str) -> int | None:
            value = usage.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        prompt_tokens = token_count("prompt_tokens")
        completion_tokens = token_count("completion_tokens")
        total_tokens = token_count("total_tokens")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        context_length = prepared.get("context_length")
        if not isinstance(context_length, int) or context_length <= 0:
            context_length = None
        saved = await asyncio.to_thread(get_store().append_exchange, cid, len(conversation["messages"]),
                                        model, prompt.strip(), answer, context_length, prompt_tokens,
                                        completion_tokens, total_tokens)
        return {"conversation": saved, "history_sent": len(history), "history_limit": HISTORY_MESSAGES}
    return r
