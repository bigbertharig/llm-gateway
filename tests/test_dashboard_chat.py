"""Dashboard chat storage, generation bridge, and LAN route scope."""

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from llm_gateway.dashboard.chat import (ChatStore, _collect_stream, _compact_tool_history,
                                        _model_tool_result, _image_attachments, _file_context,
                                        _materialize_codex_attachments, _remove_codex_attachments,
                                        context_history, shared_history_handoff, requested_page_url,
                                        malformed_reasoning, router)
from llm_gateway.lan_dashboard import _allowed
from llm_gateway.rig import RigUnavailable
from llm_gateway.manager.errors import ManagerError


def test_dashboard_hides_internal_workspace_routing_from_chat_sidebars():
    page = (Path(__file__).parents[1] / "src/llm_gateway/dashboard/static/index.html").read_text()
    assert 'data-tab="upload"' in page and 'id="tab-upload"' in page
    assert "File located here:" in page and "Copy path" in page
    assert 'timeZoneName: "short"' in page
    assert "Agent workspace" not in page
    assert 'id="chat-workspace" aria-hidden="true" tabindex="-1" hidden' in page
    assert '<h2>Availability</h2><div id="chat-rig-summary">' in page
    assert "Currently loaded" not in page
    assert "Local agent capacity" not in page
    assert "The manager controls model loading and GPU placement" not in page
    assert "box.append(activity, item)" in page


def test_codex_attachment_original_is_private_complete_and_conversation_scoped(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    cid = "a" * 32
    original = ("first section\n" + "complete evidence\n" * 900).encode()
    encoded = base64.b64encode(original).decode()
    raw = [{"name": "report.md", "type": "text/markdown",
            "data_url": "data:text/markdown;base64," + encoded}]
    converted = [{"name": "report.md", "type": "text/markdown", "method": "plain_text",
                  "text": original.decode()[:10000], "source_chars": len(original),
                  "truncated": True, "note": "Read as UTF-8 text. Content was truncated."}]

    class Codex:
        def workspace(self, value):
            assert value == str(workspace)
            return workspace

    conversation = {"cwd": str(workspace), "mode": "coding"}
    files = _materialize_codex_attachments(raw, converted, conversation, cid, Codex())
    stored = Path(files[0]["path"])
    assert stored.read_bytes() == original
    assert stored.is_relative_to(workspace / ".git" / "llm-gateway-attachments" / cid)
    assert files[0]["size_bytes"] == len(original)
    assert files[0]["sha256"] == hashlib.sha256(original).hexdigest()
    assert os.stat(stored).st_mode & 0o777 == 0o600
    context = _file_context(files)
    assert str(stored) in context
    assert "Read it in bounded sections" in context
    assert "SHA-256" in context

    _remove_codex_attachments(conversation, cid, Codex())
    assert not (workspace / ".git" / "llm-gateway-attachments" / cid).exists()


def test_shared_model_whitespace_loop_is_malformed():
    stalled = ("I'll start by searching for the relevant function.\n" + " \n" * 120) * 3
    assert malformed_reasoning(stalled)
    assert not malformed_reasoning("I inspected the relevant function and found one guarded context meter.")


def test_chat_store_persists_and_checks_concurrent_turns(tmp_path):
    root = tmp_path / "chat"
    store = ChatStore(root)
    c = store.create("small")
    image = {"name": "screen.png", "type": "image/png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
    saved = store.append_exchange(c["id"], 0, "small", "hello", "world", attachments=[image])
    assert saved["title"] == "hello"
    assert [m["role"] for m in saved["messages"]] == ["user", "assistant"]
    assert saved["messages"][0]["attachments"] == [image]
    assert saved["messages"][1]["attachments"] == []
    assert ChatStore(root).get(c["id"])["messages"][1]["content"] == "world"
    assert store.switch_model(c["id"], "small", "large")["model"] == "large"
    assert store.list()[0]["last_message_at"] == saved["messages"][-1]["created_at"]
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


def test_chat_image_validation_accepts_supported_data_urls():
    image = {"name": "screen.png", "type": "image/png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
    assert _image_attachments([image]) == [image]
    try:
        _image_attachments([{**image, "data_url": "data:text/html;base64,PHNjcmlwdD4="}])
    except ManagerError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("non-image data URL was accepted")


def test_agent_status_reconciles_a_saved_turn_without_a_live_runner(tmp_path, monkeypatch):
    root = tmp_path / "history"
    monkeypatch.setenv("CHAT_DATA_ROOT", str(root))
    store = ChatStore(root)
    conversation = store.create("Codex", provider="codex", cwd=str(tmp_path), mode="coding")
    store.set_thread(conversation["id"], "thread-stale")
    store.begin_turn(conversation["id"], 0, "Codex", "unfinished")

    class Codex:
        def status(self, thread_id):
            assert thread_id == "thread-stale"
            return {"events": [], "approvals": [], "active": False}

    class Profiles:
        def all(self):
            return {}

    app = FastAPI()
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    app.include_router(router(object(), Profiles(), None, Codex()))

    async def check():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                f"/dashboard/api/chat/conversations/{conversation['id']}/agent")
            assert response.status_code == 200
            assert response.json()["turn"]["state"] == "interrupted"
            opened = await client.get(
                f"/dashboard/api/chat/conversations/{conversation['id']}")
            assert "runner is no longer active" in opened.json()["last_turn"]["error"]

    asyncio.run(check())


def test_failed_local_turn_keeps_attachment_retry_requirement(tmp_path):
    store = ChatStore(tmp_path / "chat")
    cid = store.create("shared-nemo")["id"]
    attachment = {"name": "notes.pdf", "type": "application/pdf",
                  "method": "needs_reattach", "note": "Reattach before retry"}
    store.record_local_turn(cid, "shared-nemo", "Read this", "failed", "model failed", [attachment])
    failed = ChatStore(tmp_path / "chat").get(cid)["last_turn"]
    assert failed["state"] == "failed"
    assert failed["prompt"] == "Read this"
    assert failed["attachments"] == [attachment]
    store.record_local_turn(cid, "shared-nemo", "Read this", "completed")
    assert store.get(cid)["last_turn"]["state"] == "completed"


def test_completed_retry_shows_prior_tool_history_and_timing(tmp_path):
    store = ChatStore(tmp_path / "chat")
    cid = store.create("local-test")["id"]
    failed_id = store.begin_turn(cid, 0, "local-test", "Inspect project")
    prior_event = {"type": "tool", "name": "read_workspace_file", "result": "completed",
                   "path": "README.md", "started_ms": 100, "elapsed_ms": 3000,
                   "output": '{"content":"project notes"}'}
    store.set_turn_state(failed_id, "failed", "temporary error", [prior_event], 4000)
    retry_id = store.begin_turn(cid, 0, "local-test", "Inspect project")
    store.append_exchange(cid, 0, "local-test", "Inspect project", "Project summary",
                          turn_id=retry_id, duration_ms=1500)
    answer = store.get(cid)["messages"][-1]
    assert answer["reused_activity"] == [{**prior_event, "reused": True}]
    assert answer["timing"]["attempts"] == 2
    assert answer["timing"]["measured_tool_ms"] == 2900
    assert answer["timing"]["answer_ms"] == 1500
    assert store.get(cid)["historical_turns"] == []


def test_failed_request_stays_in_history_after_different_followup_completes(tmp_path):
    store = ChatStore(tmp_path / "chat")
    cid = store.create("Codex", mode="chat")["id"]
    failed_id = store.begin_turn(cid, 0, "Codex", "Research harness design")
    activity = [{"type": "tool", "name": "read_document", "result": "partial"}]
    store.set_turn_state(failed_id, "failed", "usage limit reached", activity, 5000)
    continue_id = store.begin_turn(cid, 0, "Codex", "continue")
    saved = store.append_exchange(cid, 0, "Codex", "continue", "Research saved to Transfer.",
                                  turn_id=continue_id)

    assert saved["last_turn"]["state"] == "completed"
    assert [turn["prompt"] for turn in saved["historical_turns"]] == ["Research harness design"]
    assert saved["historical_turns"][0]["error"] == "usage limit reached"
    assert saved["historical_turns"][0]["activity"] == activity
    assert saved["historical_turns"][0]["attachments"] == []


def test_explicit_page_url_detection():
    assert requested_page_url("Can you read [www.daedalmap.com](http://www.daedalmap.com)?") == "https://www.daedalmap.com"
    assert requested_page_url("Summarize https://example.org/docs?q=1.") == "https://example.org/docs?q=1"
    assert requested_page_url("I mentioned https://example.org but did not ask you to read it") is None
    assert requested_page_url("My website is https://example.org") is None


def test_model_handoff_keeps_saved_dialogue_within_context_budget():
    messages = [{"role": "user", "content": "earlier " + "x" * 2400},
                {"role": "assistant", "content": "later " + "y" * 2400}]
    assert context_history(messages, 4096) == messages[-1:]
    assert context_history(messages, 262144) == messages
    handoff = shared_history_handoff(messages, 4096)
    assert "1 of 2 messages" in handoff and "older content omitted" in handoff
    assert "earlier" not in handoff and "later" in handoff


def test_tool_results_sent_to_model_are_bounded_and_older_reads_compact():
    result = _model_tool_result({"path": "src/main.py", "content": "a" * 9000}, 2000)
    assert result.startswith("src/main.py\n")
    assert len(result) < 2100 and "shortened for model context" in result
    messages = [{"role": "user", "content": "Inspect"}]
    for i in range(10):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": f"call-{i}"}]},
            {"role": "tool", "tool_call_id": f"call-{i}", "content": "x" * 2000},
        ])
    _compact_tool_history(messages, 8192)
    assert sum(len(m.get("content", "")) for m in messages if m["role"] == "tool") < 10000
    assert messages[-1]["content"] == "x" * 2000
    assert any("Older tool result compacted" in m.get("content", "") for m in messages)


def test_collects_live_chat_deltas_and_usage():
    from fastapi.responses import StreamingResponse
    async def chunks():
        yield b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
        yield b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
    seen = []
    async def emit(text):
        seen.append(text)
    result = asyncio.run(_collect_stream(StreamingResponse(chunks()), emit))
    assert seen == ["Hel", "lo"]
    assert result["choices"][0]["message"]["content"] == "Hello"
    assert result["usage"]["completion_tokens"] == 2


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


def test_nemo_shared_dashboard_claims_main_then_worker_and_rejects_reloaded_context(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    from llm_gateway.dashboard import tool_runtime
    from llm_gateway.manager import models as model_library
    monkeypatch.setattr(tool_runtime, "available", lambda route, servers: (
        [], {**({"fetch_page": ([], {"properties": {"url": {}, "max_chars": {}}, "required": ["url"]})}
                if "rig_search" in servers else {}),
             **({"inspect_github_repo": ([], {"properties": {"repository": {}}, "required": ["repository"]})}
                if "github_public" in servers else {})}))
    def page_call(dispatch, name, args):
        if name == "inspect_github_repo":
            return {"structuredContent": {"repository": "example/project", "revision": "a" * 40,
                "root_entries": [{"path": "README.md", "type": "file"}],
                "readme_preview": {"content": "A sample project"}}}
        url = args["url"]
        return {"structuredContent": {"url": url, "title": url.rsplit("/", 1)[-1],
            "text": "Content from " + url, "truncated": False,
            "links": ([{"url": "https://www.daedalmap.com/about", "label": "About"},
                       {"url": "https://www.daedalmap.com/projects", "label": "Projects"}]
                      if url == "https://www.daedalmap.com" else [])}}
    monkeypatch.setattr(tool_runtime, "call", page_call)
    catalog_doc = {"load_recipes": {"recipes": {
        "nemotron74_shared_kv_http_v1": {"status": "validated", "procedure": "shared_kv_http_v1",
            "model": "nemotron-3.5-bolt:74b-a9b"}}},
        "models": {"nemotron-3.5-bolt:74b-a9b": {"status": "active"}}}
    monkeypatch.setattr(model_library, "catalog", lambda: catalog_doc)

    class Profiles:
        def all(self):
            return {}

    class Rig:
        def __init__(self):
            self.instance = "load-1"
            self.starts = 0
            self.model = "nemotron-3.5-bolt:74b-a9b"
            self.chats = []
            self.calls = []
            self.fail_attach = False
        async def status(self):
            return {"shared_kv_service": {"status": "ready" if self.chats else "stopped",
                    "model": self.model,
                    "instance_id": self.instance, "chats": [dict(row) for row in self.chats],
                    "worker_capacity": {"status": "observed", "context_capacity": 262144,
                        "free_gpu_ids": [5] if len(self.chats) < 2 else [],
                        "free_gpu_count": 1 if len(self.chats) < 2 else 0}},
                    "gpus": [{"gpu": 0, "verdict": "unleased_busy"},
                             {"gpu": 5, "verdict": "free" if len(self.chats) < 2 else "unleased_busy"}]}
        async def shared_kv(self, action, **kwargs):
            self.calls.append((action, kwargs))
            if action == "start":
                self.starts += 1
                self.instance = "load-" + str(self.starts)
                self.model = kwargs["model"]
                self.chats = [{"chat_id": "main", "gpu": 0, "context_cap": 262144, "used_tokens": 0}]
            elif action == "attach":
                if self.fail_attach:
                    raise RigUnavailable(503, "shared chat attach failed: no eligible free GPU for an attached chat")
                self.chats.append({"chat_id": kwargs["chat_id"], "gpu": 5,
                                   "context_cap": 262144, "used_tokens": 0})
            elif action == "detach":
                self.chats = [row for row in self.chats if row["chat_id"] != kwargs["chat_id"]]
            elif action == "stop":
                self.chats = []
            return {"status": "ready"}

    class Manager:
        def __init__(self):
            self.rig = Rig()
        async def chat_availability(self):
            return {"busy": False, "reason": "", "ready_profiles": []}
        async def prepare_chat_profile(self, *args):
            raise AssertionError("shared Nemo must bypass conventional profile loading")

    manager = Manager()
    seen = []
    async def shared(route, body):
        prompt = body["messages"][-1]["content"]
        seen.append((route, body["_shared_instance_id"], prompt))
        for row in manager.rig.chats:
            if route.endswith(row["chat_id"]):
                row["used_tokens"] += 20
                break
        if prompt == "Long reply":
            return JSONResponse({"choices": [{"message": {"content": "First part "},
                                              "finish_reason": "length"}]})
        if prompt == "Failing reply":
            return JSONResponse({"error": "shared chat could not produce a user-facing answer"}, status_code=502)
        if prompt == "Malformed reply":
            return JSONResponse({"choices": [{"message": {
                "content": "I will fetch more. </think> </think> </think> </think>"},
                "finish_reason": "length"}]})
        if prompt.startswith("Your previous response was malformed"):
            return JSONResponse({"choices": [{"message": {"content": "I read the pages and found two projects."},
                                              "finish_reason": "stop"}]})
        if prompt.startswith("Continue your previous answer"):
            return JSONResponse({"choices": [{"message": {"content": "and the end."},
                                              "finish_reason": "stop"}]})
        return JSONResponse({"choices": [{"message": {"content": "Nemo reply"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4}})
    app = FastAPI()
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    app.include_router(router(manager, Profiles(), None, shared_inference=shared))
    base = "/dashboard/api/chat/conversations"
    with TestClient(app) as client:
        catalog_doc["load_recipes"]["recipes"]["nemotron74_shared_kv_http_v1"]["status"] = "research"
        assert client.get("/dashboard/api/chat/options").json()["models"] == []
        catalog_doc["load_recipes"]["recipes"]["nemotron74_shared_kv_http_v1"]["status"] = "validated"
        offered = client.get("/dashboard/api/chat/options").json()["models"]
        assert offered[0]["load_estimate_seconds"] == 150
        first = client.post(base, json={"model": "shared-nemo"}).json()
        assert client.post(f"{base}/{first['id']}/prepare").json()["gpu_ids"] == [0]
        first_capacity = client.get("/dashboard/api/chat/options").json()["loaded_models"][0]
        assert first_capacity["live_chats"] == 1
        assert first_capacity["free_gpu_ids"] == [5]
        reply = client.post(f"{base}/{first['id']}/turn",
                            json={"model": "shared-nemo", "message": "Hello"})
        assert reply.status_code == 200, reply.text
        page_reply = client.post(f"{base}/{first['id']}/turn",
            json={"model": "shared-nemo", "message": "Can you read www.daedalmap.com?"})
        assert page_reply.status_code == 200, page_reply.text
        assert "Content from https://www.daedalmap.com" in seen[-1][2]
        assert page_reply.json()["conversation"]["messages"][-1]["activity"][0]["name"] == "fetch_page"
        github_reply = client.post(f"{base}/{first['id']}/turn", json={"model": "shared-nemo",
            "message": "What do you think of https://github.com/example/project?"})
        assert github_reply.status_code == 200, github_reply.text
        assert 'A sample project' in seen[-1][2]
        assert github_reply.json()["conversation"]["messages"][-1]["activity"][0]["name"] == "inspect_github_repo"
        navigation = client.post(f"{base}/{first['id']}/turn", json={"model": "shared-nemo",
            "message": "Can you follow the About and Projects links on that site?"})
        assert navigation.status_code == 200, navigation.text
        assert "Content from https://www.daedalmap.com/about" in seen[-1][2]
        assert "Content from https://www.daedalmap.com/projects" in seen[-1][2]
        assert len(navigation.json()["conversation"]["messages"][-1]["activity"]) == 3
        broad = client.post(f"{base}/{first['id']}/turn", json={"model": "shared-nemo",
            "message": "Go back to the site and explore the project. Navigate around and read more pages."})
        assert broad.status_code == 200, broad.text
        assert "Content from https://www.daedalmap.com/about" in seen[-1][2]
        assert "Content from https://www.daedalmap.com/projects" in seen[-1][2]
        assert len(broad.json()["conversation"]["messages"][-1]["activity"]) == 3
        malformed = client.post(f"{base}/{first['id']}/turn",
            json={"model": "shared-nemo", "message": "Malformed reply"})
        assert malformed.status_code == 200, malformed.text
        assert malformed.json()["conversation"]["messages"][-1]["content"] == "I read the pages and found two projects."
        continued = client.post(f"{base}/{first['id']}/turn",
                                json={"model": "shared-nemo", "message": "Long reply"})
        assert continued.status_code == 200, continued.text
        assert continued.json()["conversation"]["messages"][-1]["content"] == "First part and the end."
        second = client.post(base, json={"model": "shared-nemo"}).json()
        empty = client.post(base, json={"model": "shared-nemo", "history": "tests"}).json()
        assert client.delete(f"{base}/{empty['id']}").status_code == 200
        assert client.post(f"{base}/{second['id']}/prepare").json()["gpu_ids"] == [5]
        placements = client.get("/dashboard/api/chat/options").json()["chat_placements"]
        loaded = client.get("/dashboard/api/chat/options").json()["loaded_models"]
        assert loaded[0]["id"] == "shared-nemo" and loaded[0]["live_chats"] == 2
        assert loaded[0]["free_gpu_count"] == 0
        manager.rig.fail_attach = True
        third = client.post(base, json={"model": "shared-nemo"}).json()
        full = client.post(f"{base}/{third['id']}/prepare")
        assert full.status_code == 409
        assert "No safe placement is available for another" in full.json()["error"]
        assert "usage:" not in full.json()["error"]
        manager.rig.fail_attach = False
        attached = client.get(f"{base}/{second['id']}").json()
        assert [(p["chat_id"], p["gpu_ids"]) for p in placements] == [("main", [0]), (attached["shared_chat_id"], [5])]
        assert [item[:2] for item in seen] == [("shared-nemo-main", "load-1")] * 9
        assert [action for action, _ in manager.rig.calls] == ["start", "attach", "attach"]
        assert client.post(f"{base}/{first['id']}/park", json={
            "expected_instance_id": "load-1", "expected_chat_id": "main"}).status_code == 409
        assert client.post(f"{base}/{second['id']}/park", json={
            "expected_instance_id": "stale", "expected_chat_id": attached["shared_chat_id"]}).status_code == 409
        saved_store = ChatStore(tmp_path / "history")
        running_turn = saved_store.begin_turn(second["id"], 0, "shared-nemo", "Working task")
        assert client.post(f"{base}/{second['id']}/park", json={
            "expected_instance_id": "load-1", "expected_chat_id": attached["shared_chat_id"]}).status_code == 409
        saved_store.set_turn_state(running_turn, "interrupted")
        parked = client.post(f"{base}/{second['id']}/park", json={
            "expected_instance_id": "load-1", "expected_chat_id": attached["shared_chat_id"]})
        assert parked.status_code == 200, parked.text
        assert parked.json()["state"] == "cold"
        assert client.get(f"{base}/{second['id']}").json()["shared_chat_id"] is None
        assert client.get("/dashboard/api/chat/options").json()["loaded_models"][0]["live_chats"] == 1
        assert client.post(f"{base}/{third['id']}/prepare").status_code == 200
        manager.rig.instance = "load-2"
        manager.rig.starts = 2
        manager.rig.chats = [{"chat_id": "main", "gpu": 0, "context_cap": 262144, "used_tokens": 0}]
        stale = client.post(f"{base}/{first['id']}/turn",
                            json={"model": "shared-nemo", "message": "Continue"})
        assert stale.status_code == 200, stale.text
        assert "Saved conversation handoff" in seen[-1][2]
        catalog_doc["models"]["qwen3.6:35b-a3b-xl"] = {"status": "active"}
        assert "shared-35b" in {m["id"] for m in client.get("/dashboard/api/chat/options").json()["models"]}
        blocked = client.post(f"{base}/{first['id']}/model", json={
            "expected_model": "shared-nemo", "model": "shared-35b"})
        assert blocked.status_code == 409
        switched = client.post(f"{base}/{first['id']}/model", json={
            "expected_model": "shared-nemo", "model": "shared-35b", "reset_loaded": True})
        assert switched.status_code == 200, switched.text
        assert switched.json()["model"] == "shared-35b"
        assert len(switched.json()["messages"]) == len(stale.json()["conversation"]["messages"])
        assert [action for action, _ in manager.rig.calls][-1] == "stop"
        assert client.post(f"{base}/{first['id']}/prepare").status_code == 200
        qwen = client.post(f"{base}/{first['id']}/turn", json={"model": "shared-35b", "message": "What did we discuss?"})
        assert qwen.status_code == 200, qwen.text
        assert seen[-1][0] == "shared-35b-main"
        assert "Saved conversation handoff" in seen[-1][2]
        failed = client.post(f"{base}/{first['id']}/turn",
                             json={"model": "shared-35b", "message": "Failing reply"})
        assert failed.status_code == 502
        failed_chat = client.get(f"{base}/{first['id']}").json()
        assert failed_chat["last_turn"]["state"] == "failed"
        assert failed_chat["last_turn"]["prompt"] == "Failing reply"
        assert "user-facing answer" in failed_chat["last_turn"]["error"]
        assert len(failed_chat["messages"]) == len(qwen.json()["conversation"]["messages"])
        retried = client.post(f"{base}/{first['id']}/turn",
                              json={"model": "shared-35b", "message": "Retry"})
        assert retried.status_code == 200
        assert retried.json()["conversation"]["last_turn"]["state"] == "completed"
        reset = client.post("/dashboard/api/chat/loaded-model/reset", json={
            "model": "shared-nemo", "expected_loaded": ["shared-35b"]})
        assert reset.status_code == 200, reset.text
        assert reset.json()["reset"] == ["shared-35b"]
        assert client.post("/dashboard/api/chat/loaded-model/reset", json={
            "model": "shared-35b", "expected_loaded": ["shared-35b"]}).status_code == 409
        assert client.post(f"{base}/{first['id']}/prepare").status_code == 200
        loaded_qwen = client.get("/dashboard/api/chat/options").json()["loaded_models"][0]
        stale_unload = client.post("/dashboard/api/chat/loaded-model/unload", json={
            "model": "shared-35b", "expected_loaded": ["shared-35b"],
            "expected_instance_id": "stale-instance"})
        assert stale_unload.status_code == 409
        unloaded = client.post("/dashboard/api/chat/loaded-model/unload", json={
            "model": "shared-35b", "expected_loaded": ["shared-35b"],
            "expected_instance_id": loaded_qwen["service_instance_id"]})
        assert unloaded.status_code == 200, unloaded.text
        assert unloaded.json() == {"unloaded": "shared-35b", "remaining": []}


def test_chat_tool_intent_keeps_ordinary_question_conversational():
    from llm_gateway.dashboard.chat import chat_tool_servers
    assert chat_tool_servers("Can you help plan a 44x44 academic poster?") == frozenset()
    assert chat_tool_servers("Spawn a research agent to check this") == frozenset({"local_agents"})
    assert chat_tool_servers("Search for current poster guidelines") == frozenset({"rig_search"})
    assert chat_tool_servers("Can you read www.daedalmap.com?") == frozenset({"rig_search"})


def test_codex_prompt_is_not_interrupted_by_a_second_gateway_process(tmp_path):
    root = tmp_path / "chat"
    store = ChatStore(root)
    cid = store.create("Codex", "codex", str(tmp_path))["id"]
    pending = store.begin_turn(cid, 0, "Codex", "change the gateway")
    store.set_turn_progress(pending, [{"type": "tool", "name": "read_workspace_file",
                                       "result": "completed", "excerpt": "saved finding"}])
    assert store.get(cid)["last_turn"]["state"] == "running"
    assert store.list()[0]["last_turn_state"] == "running"
    try:
        store.begin_turn(cid, 0, "Codex", "duplicate")
    except ManagerError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("concurrent turn accepted")
    # A separate preview/router process opening the same SQLite history must not steal a live turn.
    subprocess.run([
        sys.executable, "-c",
        "from pathlib import Path; from llm_gateway.dashboard.chat import ChatStore; ChatStore(Path(__import__('sys').argv[1]))",
        str(root),
    ], check=True)
    assert store.get(cid)["last_turn"]["state"] == "running"
    with store._db() as db:
        db.execute("UPDATE turns SET runner_pid=?,runner_identity=? WHERE id=?",
                   (999_999_999, "dead-process", pending))
    reopened = ChatStore(root)
    assert reopened.get(cid)["last_turn"]["prompt"] == "change the gateway"
    assert reopened.get(cid)["last_turn"]["state"] == "interrupted"
    assert "owned this turn stopped" in reopened.get(cid)["last_turn"]["error"]
    assert reopened.get(cid)["last_turn"]["activity"][0]["excerpt"] == "saved finding"
    assert reopened.get(cid)["messages"] == []
    next_turn = reopened.begin_turn(cid, 0, "Codex", "follow up")
    saved = reopened.append_exchange(cid, 0, "Codex", "follow up", "done", turn_id=next_turn)
    assert saved["last_turn"]["state"] == "completed"
    assert [m["content"] for m in saved["messages"]] == ["follow up", "done"]
    assert reopened.get(cid)["last_turn"]["id"] != pending


def test_all_saved_conversations_remain_in_sidebar_list(tmp_path):
    store = ChatStore(tmp_path / "chat")
    for _ in range(201):
        store.create("Codex", "codex", str(tmp_path))
    assert len(store.list()) == 201


def test_local_model_uses_coding_workspace_and_coding_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "example.py").write_text("answer = 42\n")
    (workspace / "preview.png").write_bytes(b"\x89PNG\r\n\x1a\npreview")
    sibling = tmp_path / "orchestration"
    sibling.mkdir()
    profile = {"model": "local-test", "status": "active", "use": "chat", "good_for": ["chatbot"]}

    class Profiles:
        def all(self):
            return {"local-chat": profile}

    class Manager:
        async def chat_availability(self):
            return {"busy": False, "reason": "", "ready_profiles": ["local-chat"]}
        async def prepare_chat_profile(self, model, spec):
            assert (model, spec) == ("local-chat", profile)
            return {"state": "ready", "port": 11434, "context_length": 8192}

    class Codex:
        def __init__(self):
            self.workspaces = [workspace, sibling]
        def workspace(self, value):
            if value != str(workspace):
                raise ValueError("unknown workspace")
            return workspace
        def research_workspace(self, value=None):
            research = tmp_path / "research"
            research.mkdir(exist_ok=True)
            if value is not None and value != str(research):
                raise ValueError("unknown research directory")
            return research

    from llm_gateway.dashboard import tool_runtime
    seen = []
    def available(route, servers=None, root=None):
        seen.append((route, servers, root))
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        return ([{"type": "function", "function": {"name": "read_workspace_file",
                 "description": "Read a file", "parameters": schema}}],
                {"read_workspace_file": (["fixture"], schema)})
    monkeypatch.setattr(tool_runtime, "available", available)
    def call(dispatch, name, args):
        if args["path"] == str(sibling):
            raise tool_runtime.DashboardToolError(
                "Tool read_workspace_file reported an error: workspace path is not visible")
        return {"structuredContent": {"path": args["path"], "content": "answer = 42"}}
    monkeypatch.setattr(tool_runtime, "call", call)

    async def upstream(request, path, payload, port, stream, model):
        messages = json.loads(payload)["messages"]
        current_user = next(m["content"] for m in reversed(messages) if m.get("role") == "user")
        if current_user.startswith("Stop using tools."):
            assert not json.loads(payload).get("tools")
            if "Inspect then fail" in current_user:
                return JSONResponse({"error": "temporary synthesis outage"}, status_code=502)
            return JSONResponse({"choices": [{"message": {"content":
                "The file contains answer = 42; no further files were inspected."}}]})
        if "User request:\nInspect then fail" in current_user:
            if not json.loads(payload).get("tools"):
                assert "completed tool work saved from the failed attempt" in current_user.lower()
                return JSONResponse({"choices": [{"message": {"content":
                    "From the saved read, example.py contains answer = 42."}}]})
            completed = sum(m.get("role") == "tool" and "Tool call failed" not in m.get("content", "")
                            for m in messages)
            path = f"example{min(completed, 2)}.py"
            return JSONResponse({"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "repeat", "function": {"name": "read_workspace_file",
                 "arguments": json.dumps({"path": path})}}]}}]})
        if current_user.endswith("User request:\nRead another repo"):
            return JSONResponse({"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "outside", "function": {"name": "read_workspace_file",
                 "arguments": json.dumps({"path": str(sibling)})}}]}}]})
        if current_user.endswith("User request:\nInspect twice"):
            if not json.loads(payload).get("tools"):
                return JSONResponse({"choices": [{"message": {"content":
                    "The file contains answer = 42; no further files were inspected."}}]})
            return JSONResponse({"choices": [{"message": {"content": None, "tool_calls": [
                {"id": "repeat", "function": {"name": "read_workspace_file",
                 "arguments": '{"path":"example.py"}'}}]}}]})
        assert current_user.endswith("User request:\nInspect example.py")
        assert f"Coding workspace: {workspace}" in current_user
        return JSONResponse({"choices": [{"message": {"content": "answer = 42"}}]})

    app = FastAPI()
    app.add_exception_handler(ManagerError,
        lambda request, exc: JSONResponse({"error": str(exc)}, status_code=exc.status))
    app.include_router(router(Manager(), Profiles(), upstream, Codex()))
    base = "/dashboard/api/chat/conversations"
    with TestClient(app) as client:
        assert [model["id"] for model in client.get("/dashboard/api/chat/options").json()["models"]][:1] == ["local-chat"]
        assert client.post(base, json={"model": "local-chat", "mode": "coding", "cwd": str(tmp_path)}).status_code == 400
        coding = client.post(base, json={"model": "local-chat", "mode": "coding", "cwd": str(workspace)}).json()
        assert (coding["model"], coding["mode"], coding["cwd"]) == ("local-chat", "coding", str(workspace))
        assert client.post(base + f"/{coding['id']}/local/access", json={"access": "workspace"}).status_code == 200
        assert client.get(base + f"/{coding['id']}/workspace/file", params={"path": "example.py"}).json()["content"] == "answer = 42\n"
        image = client.get(base + f"/{coding['id']}/workspace/image", params={"path": "preview.png"})
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG")
        response = client.post(base + f"/{coding['id']}/turn", json={"model": "local-chat", "message": "Inspect example.py"})
        assert response.status_code == 200
        assert response.json()["conversation"]["messages"][-1]["content"] == "answer = 42"
        repeated = client.post(base + f"/{coding['id']}/turn", json={"model": "local-chat", "message": "Inspect twice"})
        assert repeated.status_code == 200, repeated.text
        finished = client.get(base + f"/{coding['id']}").json()
        assert "answer = 42" in finished["messages"][-1]["content"]
        assert [event["result"] for event in finished["messages"][-1]["activity"]] == [
            "completed", "repeated", "repeated", "repeated"]
        assert all(event["path"] == "example.py" for event in finished["messages"][-1]["activity"])
        assert '"content": "answer = 42"' in finished["messages"][-1]["activity"][0]["output"]
        failed_synthesis = client.post(base + f"/{coding['id']}/turn",
            json={"model": "local-chat", "message": "Inspect then fail"})
        assert failed_synthesis.status_code == 502
        assert len(client.get(base + f"/{coding['id']}").json()["last_turn"]["activity"]) == 6
        saved_retry = client.post(base + f"/{coding['id']}/turn",
            json={"model": "local-chat", "message": "Inspect then fail"})
        assert saved_retry.status_code == 200, saved_retry.text
        assert "saved read" in saved_retry.json()["conversation"]["messages"][-1]["content"]
        assert not saved_retry.json()["conversation"]["messages"][-1]["activity"]
        outside = client.post(base + f"/{coding['id']}/turn",
                              json={"model": "local-chat", "message": "Read another repo"})
        assert outside.status_code == 200, outside.text
        assert str(sibling) in outside.json()["conversation"]["messages"][-1]["content"]
        blocked_event = outside.json()["conversation"]["messages"][-1]["activity"][0]
        assert (blocked_event["name"], blocked_event["path"], blocked_event["result"]) == (
            "read_workspace_file", str(sibling), "blocked by workspace scope")
        assert blocked_event["elapsed_ms"] >= 0
    assert seen == [("gpu-rig/local-chat", frozenset({"rig_info", "local_agents", "workspace_read", "workspace_git"}), workspace)] * 5


def test_chat_api_uses_ready_manager_profile_and_saves_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    from llm_gateway.dashboard import chat as chat_module
    async def extracted_files(files):
        if not files:
            return []
        assert files[0]["name"] == "paper.pdf"
        return [{"name": "paper.pdf", "type": "application/pdf", "method": "pdf_text",
                 "text": "The finding is 42.", "note": "Extracted PDF text; page images omitted."}]
    monkeypatch.setattr(chat_module, "_document_attachments", extracted_files)
    monkeypatch.setattr(chat_module, "selected_items", lambda ids: ([{
        "name": "paper.pdf", "type": "application/pdf",
        "data_url": "data:application/pdf;base64,JVBERi0=", "transfer_id": ids[0]}] if ids else []))
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
            self.ready = [profile["id"]]
            self.rig = self
        async def status(self):
            return {}
        async def unload_profile(self, pid):
            self.ready.remove(pid)
            return {"unloaded": pid}
        async def chat_availability(self, profile_id=None):
            if self.busy:
                return {"busy": True, "reason": "active rig job", "ready_profiles": [], "requested_ready": False}
            return {"busy": False, "reason": "", "ready_profiles": self.ready, "requested_ready": True}
        async def prepare_chat_profile(self, pid, spec):
            if self.busy:
                raise ManagerError(409, "rig busy: active rig job", rig_busy=True)
            assert pid == profile["id"] and spec == profile
            self.loads.append(pid)
            return {"profile": pid, "state": "ready", "port": 11434, "context_length": 65536}
    manager = Manager()
    requests = []
    from llm_gateway.dashboard import tool_runtime
    tool_calls = []
    schema = {"type": "object", "properties": {"agent_type": {"type": "string"},
                                                 "task": {"type": "string"}},
              "required": ["agent_type", "task"]}
    def available(parent_route, allowed_servers=None):
        assert parent_route == "gpu-rig/" + profile["id"]
        if "local_agents" not in (allowed_servers or ()):
            return [], {}
        return ([{"type": "function", "function": {"name": "delegate_task",
                 "description": "Delegate", "parameters": schema}}],
                {"delegate_task": (["fixture"], schema)})
    def call(dispatch, name, arguments):
        assert name in dispatch
        tool_calls.append((name, arguments))
        if arguments.get("task") == "temporary failure":
            raise tool_runtime.DashboardToolError("temporary tool error")
        return {"structuredContent": {"answer": "42"}}
    monkeypatch.setattr(tool_runtime, "available", available)
    monkeypatch.setattr(tool_runtime, "call", call)
    def upstream(request):
        requests.append(json.loads(request.content))
        messages = requests[-1]["messages"]
        current_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        if current_user.startswith("Stop using tools."):
            assert not requests[-1].get("tools")
            return httpx.Response(200, json={"choices": [{"message": {"content":
                "I completed the first check and stopped after repeated tool requests."}}]})
        if current_user == "Delegate recover tool error":
            count = sum(m["role"] == "tool" for m in messages)
            if messages[-1].get("role") == "tool" and '"answer": "42"' in messages[-1]["content"]:
                return httpx.Response(200, json={"choices": [{"message": {"content": "Recovered."}}]})
            task = "temporary failure" if count == 0 else "working retry"
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"recover-{count}", "type": "function", "function": {
                    "name": "delegate_task", "arguments": json.dumps({"agent_type": "coding", "task": task})}}]}}]})
        if current_user in ("Delegate 3 checks", "Delegate 8 checks", "Delegate forever"):
            if not requests[-1].get("tools"):
                return httpx.Response(200, json={"choices": [{"message": {"content":
                    "I completed the first check and stopped after repeated tool requests."}}]})
            if current_user == "Delegate 8 checks":
                count = sum(m["role"] == "tool" for m in messages)
                if count == 8:
                    return httpx.Response(200, json={"choices": [{"message": {"content": "Eight checks finished."}}]})
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": f"call-{count}", "type": "function", "function": {
                        "name": "delegate_task", "arguments": json.dumps({"agent_type": "coding", "task": f"check {count}"})}}]}}]})
            if current_user == "Delegate forever" or sum(m["role"] == "tool" for m in messages) < 3:
                assert "tools" in requests[-1]
                count = sum(m["role"] == "tool" for m in messages)
                task = "7 times 6" if current_user == "Delegate forever" else f"check {count}"
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "call-next", "type": "function", "function": {
                        "name": "delegate_task", "arguments": json.dumps({"agent_type": "coding", "task": task})}}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Three checks finished."}}]})
        if requests[-1]["messages"][-1].get("content") == "Delegate this":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "delegate_task", "arguments": '{"agent_type":"coding","task":"7 times 6"}'}}]}}]})
        if requests[-1]["messages"][-1].get("role") == "tool":
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                "content": "The child answered 42."}}]})
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
        if stream:
            from fastapi.responses import StreamingResponse
            assert body["stream_options"] == {"include_usage": True}
            async def events():
                yield b'data: {"choices":[{"delta":{"content":"Streamed "}}]}\n\n'
                yield b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
                yield b'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}\n\n'
                yield b'data: [DONE]\n\n'
            return StreamingResponse(events(), media_type="text/event-stream")
        upstream_response = upstream(httpx.Request("POST", "http://rig/v1/chat/completions", content=raw))
        from fastapi.responses import Response
        return Response(upstream_response.content, status_code=upstream_response.status_code,
                        media_type="application/json")
    app.include_router(router(manager, Profiles(), forward))
    with TestClient(app) as client:
        opts = client.get("/dashboard/api/chat/options").json()
        assert opts["models"][0]["id"] == profile["id"]
        assert opts["models"][0]["native_inputs"] == ["text"]
        assert opts["models"][0]["converted_inputs"] == ["pdf", "text_file"]
        assert "image/png" in opts["codex_attachment_capabilities"]["native_inputs"]
        assert [model["id"] for model in opts["models"]][:2] == [profile["id"], other["id"]]
        c = client.post("/dashboard/api/chat/conversations", json={"model": profile["id"]}).json()
        test_chat = client.post("/dashboard/api/chat/conversations",
                                json={"model": profile["id"], "history": "tests"}).json()
        assert client.get("/dashboard/api/chat/conversations").json()["conversations"][0]["id"] == c["id"]
        assert [row["id"] for row in client.get("/dashboard/api/chat/conversations?history=tests").json()["conversations"]] == [test_chat["id"]]
        assert client.get(f"/dashboard/api/chat/conversations/{test_chat['id']}").json()["id"] == test_chat["id"]
        assert (tmp_path / "history" / "tests" / "conversations.sqlite3").is_file()
        assert client.post("/dashboard/api/chat/conversations",
                           json={"model": profile["id"], "history": "unknown"}).status_code == 400
        prepared = client.post(f"/dashboard/api/chat/conversations/{c['id']}/prepare", json={})
        assert prepared.status_code == 200
        assert prepared.json()["state"] == "ready"
        assert manager.loads == [profile["id"]]
        assert client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["messages"] == []
        out = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                          json={"model": profile["id"], "message": "Hi"}).json()
        assert out["conversation"]["messages"][-1]["content"] == "Hello from the model."
        assert out["conversation"]["messages"][-1]["duration_ms"] >= 0
        assert out["conversation"]["last_turn"]["duration_ms"] >= 0
        assert out["conversation"]["context_length"] == 65536
        assert out["conversation"]["last_prompt_tokens"] == 42
        assert out["conversation"]["last_completion_tokens"] == 9
        assert out["conversation"]["last_total_tokens"] == 51
        assert client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["last_total_tokens"] == 51
        assert manager.loads == [profile["id"], profile["id"]]
        document = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
            json={"model": profile["id"], "message": "", "transfers": ["a" * 32]})
        assert document.status_code == 200, document.text
        uploaded = document.json()["conversation"]["messages"][-2]
        assert uploaded["content"] == "Please analyze the attached file."
        assert uploaded["attachments"][0]["note"] == "Extracted PDF text; page images omitted."
        assert "The finding is 42." in requests[-1]["messages"][-1]["content"]
        assert "page images omitted" in requests[-1]["messages"][-1]["content"]
        manager.busy = True
        assert client.get("/dashboard/api/chat/options").json()["rig_busy"] is True
        blocked = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                              json={"model": profile["id"], "message": "blocked"})
        assert blocked.status_code == 409
        assert manager.loads == [profile["id"], profile["id"], profile["id"]]
        assert client.post(f"/dashboard/api/chat/conversations/{c['id']}/prepare", json={}).status_code == 409
        assert len(client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["messages"]) == 4
        manager.busy = False
        assert requests[0]["messages"] == [{"role": "user", "content": "Hi"}]
        delegated = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                                json={"model": profile["id"], "message": "Delegate this"}).json()
        assert delegated["conversation"]["messages"][-1]["content"] == "The child answered 42."
        delegated_event = delegated["conversation"]["messages"][-1]["activity"][0]
        assert (delegated_event["name"], delegated_event["result"]) == ("delegate_task", "completed")
        assert "42" in delegated_event["excerpt"]
        assert tool_calls == [("delegate_task", {"agent_type": "coding", "task": "7 times 6"})]
        multi = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                            json={"model": profile["id"], "message": "Delegate 3 checks"})
        assert multi.status_code == 200, multi.text
        assert multi.json()["conversation"]["messages"][-1]["content"] == "Three checks finished."
        assert len(multi.json()["conversation"]["messages"][-1]["activity"]) == 3
        extended = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                               json={"model": profile["id"], "message": "Delegate 8 checks"})
        assert extended.status_code == 200, extended.text
        assert extended.json()["conversation"]["messages"][-1]["content"] == "Eight checks finished."
        assert len(extended.json()["conversation"]["messages"][-1]["activity"]) == 8
        streamed_tools = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
            json={"model": profile["id"], "message": "Delegate 3 checks", "stream": True})
        assert streamed_tools.status_code == 200, streamed_tools.text
        assert streamed_tools.text.count('"type": "activity"') == 3
        assert '"output"' in streamed_tools.text
        assert '"type": "done"' in streamed_tools.text
        recovered = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                                json={"model": profile["id"], "message": "Delegate recover tool error"})
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["conversation"]["messages"][-1]["content"] == "Recovered."
        assert [event["result"] for event in recovered.json()["conversation"]["messages"][-1]["activity"]] == [
            "failed", "completed"]
        assert any("temporary tool error" in (m.get("content") or "")
                   for request_body in requests for m in request_body["messages"])
        assert not any("Tool unavailable" in (m.get("content") or "")
                       for m in requests[-1]["messages"])
        limit = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                            json={"model": profile["id"], "message": "Delegate forever"})
        assert limit.status_code == 200, limit.text
        finished = client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()
        assert "first check" in finished["messages"][-1]["content"]
        assert [event["result"] for event in finished["messages"][-1]["activity"]] == [
            "completed", "repeated", "repeated", "repeated"]
        streamed = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
            json={"model": profile["id"], "message": "Answer briefly", "stream": True})
        assert streamed.status_code == 200, streamed.text
        assert '"type": "delta"' in streamed.text
        assert '"type": "done"' in streamed.text
        assert client.get(f"/dashboard/api/chat/conversations/{c['id']}").json()["messages"][-1]["content"] == "Streamed answer"
        image = {"name": "screen.png", "type": "image/png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
        image_blocked = client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                                    json={"model": profile["id"], "message": "look", "images": [image]})
        assert image_blocked.status_code == 400
        assert client.post(f"/dashboard/api/chat/conversations/{c['id']}/turn",
                           json={"model": other["id"], "message": "Hi"}).status_code == 409
        switched = client.post(f"/dashboard/api/chat/conversations/{c['id']}/model",
                               json={"expected_model": profile["id"], "model": other["id"], "reset_loaded": True})
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
        test_turn = client.post(f"/dashboard/api/chat/conversations/{test_chat['id']}/turn",
                                json={"model": profile["id"], "message": "test history turn"})
        assert test_turn.status_code == 200
        assert test_turn.json()["conversation"]["messages"][-1]["content"] == "Hello from the model."
        assert client.get("/dashboard/api/chat/conversations").json()["conversations"] == []
        assert client.get("/dashboard/api/chat/conversations?history=tests").json()["conversations"][0]["id"] == test_chat["id"]


def test_lan_chat_routes_are_explicit():
    cid = "a" * 32
    assert _allowed("GET", "/dashboard/api/chat/options")
    assert _allowed("POST", "/dashboard/api/chat/conversations")
    assert _allowed("GET", "/dashboard/api/transfers")
    assert _allowed("POST", "/dashboard/api/transfers")
    assert _allowed("GET", "/dashboard/api/transfers/" + "a" * 32)
    assert _allowed("DELETE", "/dashboard/api/transfers/" + "a" * 32)
    assert not _allowed("POST", "/dashboard/api/transfers/" + "a" * 32)
    assert not _allowed("GET", "/dashboard/api/transfers/not-an-id")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/turn")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/model")
    assert _allowed("POST", "/dashboard/api/chat/loaded-model/reset")
    assert _allowed("POST", "/dashboard/api/chat/loaded-model/unload")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/prepare")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/park")
    assert _allowed("DELETE", f"/dashboard/api/chat/conversations/{cid}")
    assert _allowed("GET", "/dashboard/api/chat/codex/account")
    assert _allowed("GET", "/v1/manager/local-agent-capabilities")
    assert _allowed("POST", "/v1/manager/agent-routes/release")
    assert _allowed("POST", "/dashboard/api/chat/codex/login")
    assert _allowed("POST", "/dashboard/api/chat/codex/logout")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/agent")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/agent/approval")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/agent/command")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/agent/steer")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/agent/interrupt")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/workspace/tree")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/workspace/file")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/workspace/image")
    assert _allowed("GET", f"/dashboard/api/chat/conversations/{cid}/workspace/search")
    assert _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/workspace/file")
    assert not _allowed("POST", f"/dashboard/api/chat/conversations/{cid}/workspace/tree")
    assert not _allowed("GET", "/v1/chat/completions")
    assert not _allowed("POST", "/dashboard/api/chat/admin")


def test_codex_chat_uses_account_workspace_and_separate_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAT_DATA_ROOT", str(tmp_path / "history"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (workspace / "example.py").write_text("answer = 42\n")
    from llm_gateway.dashboard import chat as chat_module
    document_bytes = b"beginning only\n" + b"full report evidence\n" * 1000
    document = {"name": "report.md", "type": "text/markdown",
                "data_url": "data:text/markdown;base64," + base64.b64encode(document_bytes).decode(),
                "transfer_id": "d" * 32}
    async def extracted_files(files):
        if not files:
            return []
        return [{"name": files[0]["name"], "type": "text/markdown", "method": "plain_text",
                 "text": "beginning only", "source_chars": 20000, "truncated": True,
                 "note": "Read as UTF-8 text. Content was truncated to 10,000 characters."}]
    monkeypatch.setattr(chat_module, "_document_attachments", extracted_files)
    monkeypatch.setattr(chat_module, "selected_items", lambda ids: [document] if ids else [])

    class Codex:
        def __init__(self):
            self.workspaces = [workspace]
            self.calls = []
            self.images = []
            self.deleted = []
            self.steers = []
            self.interrupts = []
            self.gate = threading.Event()
            self.gate.set()
        def workspace(self, value):
            if value != str(workspace):
                raise ValueError("unknown workspace")
            return workspace
        def research_workspace(self):
            return tmp_path / "research"
        async def account(self):
            return {"connected": True, "email": "operator@example.test", "plan": "plus"}
        async def start_login(self):
            return {"verification_url": "https://auth.openai.com/codex/device", "user_code": "TEST-1234"}
        async def logout(self):
            return None
        async def turn(self, thread_id, prompt, cwd, on_thread, images=None, model=None,
                       effort=None, reviewer=None, mode="coding", access="local",
                       conversation_id=None):
            tid = thread_id or f"thread-{len(self.calls) + 1}"
            await on_thread(tid)
            await asyncio.to_thread(self.gate.wait)
            self.calls.append((tid, prompt, cwd, mode))
            self.images.append(images or [])
            return tid, "Codex answer: " + prompt
        def status(self, thread_id):
            return {"events": [], "approvals": []}
        async def steer(self, thread_id, prompt):
            self.steers.append((thread_id, prompt))
            return "active-turn"
        async def interrupt(self, thread_id):
            self.interrupts.append(thread_id)
            return "active-turn"
        async def decide(self, thread_id, request_id, decision):
            return None
        async def delete_thread(self, thread_id):
            self.deleted.append(thread_id)
        async def compact(self, thread_id):
            self.compacted = thread_id
        async def models(self):
            return [{"model": "test-codex", "displayName": "Test Codex"}]

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
        research = client.post("/dashboard/api/chat/conversations", json={"model": "Codex"}).json()
        assert research["mode"] == "chat"
        assert research["cwd"] == str(tmp_path / "research")
        assert client.get(f"/dashboard/api/chat/conversations/{research['id']}/workspace/tree").status_code == 400
        first = client.post("/dashboard/api/chat/conversations",
                            json={"model": "Codex", "cwd": str(workspace), "mode": "coding"}).json()
        second = client.post("/dashboard/api/chat/conversations",
                             json={"model": "Codex", "cwd": str(workspace), "mode": "coding"}).json()
        assert first["provider"] == second["provider"] == "codex"
        assert first["mode"] == second["mode"] == "coding"
        base = f"/dashboard/api/chat/conversations/{first['id']}/workspace"
        assert client.get(base + "/tree").json()["entries"][0]["name"] == "example.py"
        opened = client.get(base + "/file", params={"path": "example.py"}).json()
        assert opened["content"] == "answer = 42\n"
        assert client.get(base + "/search", params={"q": "answer"}).json()["matches"][0]["line"] == 1
        assert client.post(base + "/file", json={"path": "example.py", "content": "answer = 43\n",
                                                 "expected_sha256": opened["sha256"]}).status_code == 200
        assert client.post(base + "/file", json={"path": "example.py", "content": "stale",
                                                 "expected_sha256": opened["sha256"]}).status_code == 409
        image = {"name": "screen.png", "type": "image/png", "data_url": "data:image/png;base64,iVBORw0KGgo="}
        for index, (cid, message) in enumerate(((first["id"], "first"), (second["id"], "second"),
                                                (first["id"], "follow up"))):
            if index == 0:
                cloud.gate.clear()
            response = client.post(f"/dashboard/api/chat/conversations/{cid}/turn",
                                   json={"model": "Codex", "message": message,
                                         "images": [image] if index == 0 else [],
                                         "transfers": [document["transfer_id"]] if index == 0 else []})
            assert response.status_code == 200
            assert response.json()["turn_id"]
            if index == 0:
                try:
                    assert client.get(f"/dashboard/api/chat/conversations/{cid}").json()["last_turn"]["state"] == "running"
                    assert client.post(f"/dashboard/api/chat/conversations/{cid}/turn",
                                       json={"model": "Codex", "message": "duplicate"}).status_code == 409
                    steered = client.post(f"/dashboard/api/chat/conversations/{cid}/agent/steer",
                                          json={"message": "Focus on the dashboard UI"})
                    assert steered.status_code == 200
                    assert steered.json()["accepted"]
                    settings = client.post(f"/dashboard/api/chat/conversations/{cid}/codex/settings",
                                           json={"model": "test-codex", "effort": None,
                                                 "reviewer": "user", "access": "local"})
                    assert settings.status_code == 200
                    stopped = client.post(f"/dashboard/api/chat/conversations/{cid}/agent/interrupt", json={})
                    assert stopped.status_code == 200
                finally:
                    cloud.gate.set()
            deadline = time.monotonic() + 3
            while client.get(f"/dashboard/api/chat/conversations/{cid}").json()["last_turn"]["state"] == "running":
                assert time.monotonic() < deadline
                time.sleep(.01)
        assert [call[0] for call in cloud.calls] == ["thread-1", "thread-2", "thread-1"]
        assert all(call[3] == "coding" for call in cloud.calls)
        response = client.post(f"/dashboard/api/chat/conversations/{research['id']}/turn",
                               json={"model": "Codex", "message": "Research this"})
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while client.get(f"/dashboard/api/chat/conversations/{research['id']}").json()["last_turn"]["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert cloud.calls[-1][3] == "chat"
        assert cloud.images[0] == [image]
        first_saved = client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()
        stored = Path(first_saved["messages"][0]["attachments"][1]["path"])
        assert stored.read_bytes() == document_bytes
        assert stored.is_relative_to(workspace / ".git" / "llm-gateway-attachments" / first["id"])
        assert str(stored) in cloud.calls[0][1]
        assert "Read it in bounded sections" in cloud.calls[0][1]
        assert cloud.steers == [("thread-1", "Focus on the dashboard UI")]
        assert cloud.interrupts == ["thread-1"]
        assert client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()["messages"][1]["content"] == "Focus on the dashboard UI"
        assert client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()["messages"][0]["attachments"][0] == image
        assert "attachments" not in client.get(f"/dashboard/api/chat/conversations/{first['id']}/agent").json()["turn"]
        assert client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()["thread_id"] == "thread-1"
        assert client.post(f"/dashboard/api/chat/conversations/{first['id']}/prepare", json={}).status_code == 400
        assert client.post(f"/dashboard/api/chat/conversations/{first['id']}/agent/command",
                           json={"command": "compact"}).status_code == 200
        assert cloud.compacted == "thread-1"
        models = client.post(f"/dashboard/api/chat/conversations/{first['id']}/agent/command",
                             json={"command": "model"}).json()
        assert "test-codex" in models["message"]
        assert client.post(f"/dashboard/api/chat/conversations/{first['id']}/agent/command",
                           json={"command": "model", "value": "test-codex"}).status_code == 200
        assert client.get(f"/dashboard/api/chat/conversations/{first['id']}").json()["codex_model"] == "test-codex"
        assert client.post(f"/dashboard/api/chat/conversations/{first['id']}/agent/command",
                           json={"command": "delete"}).status_code == 400
        assert client.delete(f"/dashboard/api/chat/conversations/{second['id']}").status_code == 200
        assert cloud.deleted == ["thread-2"]
        assert client.delete(f"/dashboard/api/chat/conversations/{first['id']}").status_code == 200
        assert cloud.deleted == ["thread-2", "thread-1"]
        assert not stored.exists()
