"""Dashboard chat storage, generation bridge, and LAN route scope."""

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from llm_gateway.dashboard.chat import ChatStore, router
from llm_gateway.lan_dashboard import _allowed
from llm_gateway.rig import RigUnavailable
from llm_gateway.manager.errors import ManagerError


def test_chat_store_persists_and_checks_concurrent_turns(tmp_path):
    root = tmp_path / "chat"
    store = ChatStore(root)
    c = store.create("small")
    saved = store.append_exchange(c["id"], 0, "small", "hello", "world")
    assert saved["title"] == "hello"
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert ChatStore(root).get(c["id"])["messages"][1]["content"] == "world"
    assert store.switch_model(c["id"], "small", "large")["model"] == "large"
    try:
        store.append_exchange(c["id"], 2, "small", "stale model", "answer")
    except Exception as exc:
        assert "model changed" in str(exc)
    else:
        raise AssertionError("turn overwrote an explicit model switch")
    try:
        store.switch_model(c["id"], "small", "another")
    except Exception as exc:
        assert "changed in another tab" in str(exc)
    else:
        raise AssertionError("stale model switch accepted")
    try:
        store.append_exchange(c["id"], 0, "large", "stale", "answer")
    except Exception as exc:
        assert "changed in another tab" in str(exc)
    else:
        raise AssertionError("stale turn accepted")
    store.delete(c["id"])
    assert store.list() == []
    assert root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_chat_store_migrates_existing_conversations(tmp_path):
    import sqlite3

    root = tmp_path / "chat"
    root.mkdir()
    db = sqlite3.connect(root / "conversations.sqlite3")
    db.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO conversations VALUES (?,?,?,?,?)",
        ("older", "Older chat", "small", "2026-11-01", "2026-11-01"),
    )
    db.commit()
    db.close()
    store = ChatStore(root)
    assert store.get("older")["last_total_tokens"] is None
    assert store.create("small")["context_length"] is None
    saved = store.append_exchange("older", 0, "small", "hi", "hello", 16384, 12, 4, 16)
    assert (saved["context_length"], saved["last_total_tokens"]) == (16384, 16)


def test_chat_api_uses_ready_manager_profile_and_saves_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    profile = {"id": "harness-gemma4-e4b-64k", "model": "gemma-test", "status": "active", "use": "chat", "good_for": ["chatbot"]}
    other = {"id": "gpt-oss-20b", "model": "other-test", "status": "active", "use": "chat", "good_for": ["chatbot"]}
    unmarked = {"id": "code-only", "model": "code-test", "status": "active", "use": "chat", "good_for": ["coding"]}
    class Profiles:
        def all(self):
            return {profile["id"]: profile, other["id"]: other, unmarked["id"]: unmarked}
    class Manager:
        def __init__(self):
            self.loads = []
            self.busy = False
        async def chat_availability(self, profile_id=None):
            if self.busy:
                return {"busy": True, "reason": "active rig job", "ready_profiles": [], "requested_ready": False}
            return {"busy": False, "reason": "", "ready_profiles": [profile["id"]], "requested_ready": True}
        async def prepare_chat_profile(self, pid, spec):
            if self.busy:
                raise ManagerError(409, "rig busy: active rig job", rig_busy=True)
            assert pid == profile["id"] and spec == profile
            self.loads.append(pid)
            return {"profile": pid, "state": "ready", "port": 11434, "context_length": 65536}
    manager = Manager()
    requests = []
    def upstream(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "Hello from the model."}}],
                                         "usage": {"prompt_tokens": 42, "completion_tokens": 9, "total_tokens": 51}})
    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            app.state.http = client
            yield
    app = FastAPI(lifespan=lifespan)
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    async def forward(request, path, raw, port, stream, profile_id):
        assert path == "/v1/chat/completions"
        assert port == 11434
        assert profile_id == profile["id"]
        body = json.loads(raw)
        assert body["model"] == "gpu-rig/" + profile["id"]
        upstream_response = upstream(httpx.Request("POST", "http://rig/v1/chat/completions", content=raw))
        from fastapi.responses import Response
        return Response(upstream_response.content, status_code=upstream_response.status_code,
                        media_type="application/json")
    app.include_router(router(manager, Profiles(), forward))
    with TestClient(app) as client:
        opts = client.get("/dashboard/api/chat/options").json()
        assert opts["models"][0]["id"] == profile["id"]
        assert [model["id"] for model in opts["models"]] == [profile["id"], other["id"]]
        c = client.post("/dashboard/api/chat/conversations", json={"model": profile["id"]}).json()
        prepared = client.post(f"/dashboard/api/chat/conversations/{c['id']}/prepare", json={})
        assert prepared.status_code == 200
        assert prepared.json()["state"] == "ready"
        assert manager.loads == [profile["id"]]
        assert client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["messages"] == []
        out = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                          json={"model": profile["id"], "message": "Hi"}).json()
        assert out["conversation"]["messages"][-1]["content"] == "Hello from the model."
        assert out["conversation"]["context_length"] == 65536
        assert out["conversation"]["last_prompt_tokens"] == 42
        assert out["conversation"]["last_completion_tokens"] == 9
        assert out["conversation"]["last_total_tokens"] == 51
        assert client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["last_total_tokens"] == 51
        assert manager.loads == [profile["id"], profile["id"]]
        manager.busy = True
        assert client.get("/dashboard/api/chat/options").json()["rig_busy"] is True
        blocked = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                              json={"model": profile["id"], "message": "blocked"})
        assert blocked.status_code == 409
        assert manager.loads == [profile["id"], profile["id"]]
        assert client.post(f"/dashboard/api/chat/conversations/{c['id']}/prepare", json={}).status_code == 409
        assert len(client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["messages"]) == 2
        manager.busy = False
        assert requests[0]["messages"] == [{"role": "user", "content": "Hi"}]
        assert client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                           json={"model": other["id"], "message": "Hi"}).status_code == 409
        switched = client.post(f"/dashboard/api/chat/conversations/{c['id']}/model",
                               json={"expected_model": profile["id"], "model": other["id"]})
        assert switched.status_code == 200
        assert switched.json()["model"] == other["id"]
        assert switched.json()["last_total_tokens"] is None
        assert client.post(f"/dashboard/api/chat/conversations/{c['id']}/model",
                           json={"expected_model": profile["id"], "model": other["id"]}).status_code == 409
        assert client.get("/dashboard/api/chat/conversations").json()["conversations"][0]["id"] == c["id"]
        async def rig_down(profile_id=None):
            raise RigUnavailable(502, "rig offline")
        manager.chat_availability = rig_down
        assert client.get("/dashboard/api/chat/options").json()["rig_available"] is False
        assert client.get("/dashboard/api/chat/conversations").json()["conversations"][0]["id"] == c["id"]
        assert client.delete(f"/dashboard/api/chat/conversations/{c['id']}").status_code == 200


def test_lan_chat_routes_are_explicit():
    cid = "a" * 32
    assert _allowed("GET", "/dashboard/api/chat/options")
    assert _allowed("POST", "/dashboard/api/chat/conversations")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/turn")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/model")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/prepare")
    assert _allowed("DELETE", f"/dashboard/api/chat/conversations/{cid}")
    assert _allowed("GET", "/dashboard/api/chat/codex/account")
    assert _allowed("POST", "/dashboard/api/chat/codex/login")
    assert _allowed("POST", "/dashboard/api/chat/codex/logout")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/agent")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/agent/approval")
    assert not _allowed("GET", "/v1/chat/completions")
    assert not _allowed("POST", "/dashboard/api/chat/admin")


def test_codex_chat_uses_account_workspace_and_separate_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    workspace = tmp_path / "repo"
    workspace.mkdir()

    class Codex:
        def __init__(self):
            self.workspaces = [workspace]
            self.calls = []
            self.deleted = []
        def workspace(self, value):
            if value != str(workspace):
                raise ValueError("unknown workspace")
            return workspace
        async def account(self):
            return {"connected": True, "email": "operator@example.test", "plan": "plus"}
        async def start_login(self):
            return {"verification_url": "https://auth.openai.com/codex/device", "user_code": "TEST-1234"}
        async def logout(self):
            return None
        async def turn(self, thread_id, prompt, cwd, on_thread):
            tid = thread_id or f"thread-{len(self.calls) + 1}"
            await on_thread(tid)
            self.calls.append((tid, prompt, cwd))
            return tid, "Codex answer: " + prompt
        def status(self, thread_id):
            return {"events": [], "approvals": []}
        async def decide(self, thread_id, request_id, decision):
            return None
        async def delete_thread(self, thread_id):
            self.deleted.append(thread_id)

    class Profiles:
        def all(self):
            return {}
    class Manager:
        async def chat_availability(self):
            return {"busy": False, "reason": "", "ready_profiles": []}
    cloud = Codex()
    app = FastAPI()
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    app.include_router(router(Manager(), Profiles(), None, cloud))
    with TestClient(app) as client:
        assert client.get("/dashboard/api/chat/codex/account").json()["connected"]
        first = client.post("/dashboard/api/chat/conversations",
                            json={"model": "Codex", "cwd": str(workspace)}).json()
        second = client.post("/dashboard/api/chat/conversations",
                             json={"model": "Codex", "cwd": str(workspace)}).json()
        assert first["provider"] == second["provider"] == "codex"
        for cid, message in ((first["id"], "first"), (second["id"], "second"), (first["id"], "follow up")):
            response = client.post(f"/dashboard/api/chat/conversations/{cid}/turn",
                                   json={"model": "Codex", "message": message})
            assert response.status_code == 200
        assert [call[0] for call in cloud.calls] == ["thread-1", "thread-2", "thread-1"]
        assert client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()["thread_id"] == "thread-1"
        assert client.post(f"/dashboard/api/chat/conversations/{first['id']}/prepare", json={}).status_code == 400
        assert client.delete(f"/dashboard/api/chat/conversations/{second['id']}").status_code == 200
        assert cloud.deleted == ["thread-2"]
