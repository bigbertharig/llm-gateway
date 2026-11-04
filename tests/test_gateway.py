"""Gateway tests: fake `rig` command (subprocess) + fake llama-server (httpx MockTransport)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from llm_gateway.app import create_app, translate_upstream_error
from llm_gateway.rig import Rig, RigUnavailable
from llm_gateway.settings import Settings, read_api_keys

UPSTREAM_PORT = 11499

# State in $FAKE_RIG_STATE; `start` behavior from $FAKE_RIG_START (ok|busy|nocap|fail|locked-once|slow).
# $FAKE_RIG_SSH=down makes every call exit 255, as ssh does when the rig is unreachable.
FAKE_RIG = textwrap.dedent("""\
    import json, os, sys, time
    if os.environ.get("FAKE_RIG_SSH") == "down":
        print("ssh: connect to host 10.0.0.3 port 22: No route to host"); sys.exit(255)
    state_path = os.environ["FAKE_RIG_STATE"]
    st = json.load(open(state_path))
    args = sys.argv[1:]
    if args[:2] == ["status", "--json"]:
        print(json.dumps(st)); sys.exit(0)
    if args[:2] == ["start", "remote"]:
        with open(state_path + ".starts", "a") as f: f.write(args[2] + "\\n")
        mode = os.environ.get("FAKE_RIG_START", "ok")
        if mode == "busy": print("busy: bench (ready, since t, from 10.0.0.2)"); sys.exit(2)
        if mode == "nocap": print("error: no capacity for x"); sys.exit(4)
        if mode == "fail": print("FAILED: container exited"); sys.exit(1)
        if mode == "locked-once" and not os.path.exists(state_path + ".locked"):
            open(state_path + ".locked", "w").close(); print("locked"); sys.exit(3)
        time.sleep(2.5 if mode == "slow" else 0.5)
        st["profiles"].append({"id": args[2], "port": %d, "state": "ready"})
        json.dump(st, open(state_path, "w")); sys.exit(0)
    sys.exit(64)
""" % UPSTREAM_PORT)


def fake_llama(request: httpx.Request) -> httpx.Response:
    assert request.url.port == UPSTREAM_PORT
    body = json.loads(request.content)
    if request.url.path == "/tokenize":
        return httpx.Response(200, json={"tokens": list(range(len(body["content"].split())))})
    if "OVERFLOW" in json.dumps(body):
        return httpx.Response(400, json={"error": {
            "code": 400, "message": "request (20030 tokens) exceeds the available context size (16384 tokens)",
            "type": "exceed_context_size_error", "n_prompt_tokens": 20030, "n_ctx": 16384}})
    if body.get("stream"):
        chunks = [b'data: {"choices":[{"delta":{"content":"o"}}]}\n\n',
                  b'data: {"choices":[{"delta":{"content":"k"}}]}\n\n', b"data: [DONE]\n\n"]
        async def gen():
            for c in chunks:
                yield c
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "echo_model": body.get("model")})


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "rig.py").write_text(FAKE_RIG)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"mode": "idle", "status": "idle", "profiles": []}))
    lib = tmp_path / "lib.json"
    lib.write_text(json.dumps({"remote_profiles": [
        {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 16384, "description": "small"},
        {"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536, "description": "big"},
    ]}))
    monkeypatch.setenv("FAKE_RIG_STATE", str(state))
    monkeypatch.setenv("FAKE_RIG_START", "ok")
    monkeypatch.setattr("llm_gateway.rig.LOCKED_RETRY_SECONDS", 0.1)
    settings = Settings(
        rig_command=(sys.executable, str(tmp_path / "rig.py")), profiles_path=lib,
        upstream_host="127.0.0.1", model_prefix="gpu-rig", bind="127.0.0.1", port=0,
        start_timeout_seconds=30, request_timeout_seconds=30,
    )

    def starts():
        p = Path(str(state) + ".starts")
        return p.read_text().split() if p.exists() else []

    return {"settings": settings, "lib": lib, "starts": starts}


@pytest.fixture
def client(env):
    app = create_app(env["settings"], upstream_transport=httpx.MockTransport(fake_llama))
    with TestClient(app) as c:
        yield c


def chat(client, model="gpu-rig/coder-7b", **extra):
    return client.post("/v1/chat/completions",
                       json={"model": model, "messages": [{"role": "user", "content": "hi"}], **extra})


def test_models_lists_profiles_with_context(client):
    models = {m["id"]: m for m in client.get("/v1/models").json()["data"]}
    assert set(models) == {"gpu-rig/coder-7b", "gpu-rig/chat-27b"}
    assert models["gpu-rig/chat-27b"]["context_length"] == 65536


def test_models_follow_library_edits(client, env):
    lib = json.loads(env["lib"].read_text())
    lib["remote_profiles"].append({"id": "new-one", "model": "m", "ctx_size": 4096})
    env["lib"].write_text(json.dumps(lib))
    os.utime(env["lib"], (time.time() + 5, time.time() + 5))
    assert "gpu-rig/new-one" in [m["id"] for m in client.get("/v1/models").json()["data"]]


def test_unknown_model_404_lists_published(client):
    r = chat(client, model="gpu-rig/nope")
    assert r.status_code == 404
    assert "gpu-rig/coder-7b" in r.json()["error"]["message"]


def test_cold_start_loads_then_forwards_body_unchanged(client, env):
    r = chat(client)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert r.json()["echo_model"] == "gpu-rig/coder-7b"
    assert env["starts"]() == ["coder-7b"]


def test_warm_request_does_not_start(client, env):
    chat(client)
    chat(client)
    assert env["starts"]() == ["coder-7b"]


def test_streaming_passthrough(client):
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "gpu-rig/coder-7b", "stream": True, "messages": []}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        data = b"".join(r.iter_bytes())
    assert b'"content":"o"' in data and data.rstrip().endswith(b"data: [DONE]")


@pytest.mark.parametrize("stream", [False, True])
def test_context_overflow_translated_to_openai_shape(client, stream):
    r = client.post("/v1/chat/completions", json={
        "model": "gpu-rig/coder-7b", "stream": stream, "messages": [{"role": "user", "content": "OVERFLOW"}]})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "context_length_exceeded"
    assert err["type"] == "invalid_request_error"
    assert "maximum context length is 16384 tokens" in err["message"]
    assert err["n_prompt_tokens"] == 20030


def test_other_upstream_errors_pass_through():
    raw = json.dumps({"error": {"type": "server_error", "message": "x"}}).encode()
    assert translate_upstream_error(500, raw) == (500, raw)
    assert translate_upstream_error(400, raw) == (400, raw)
    assert translate_upstream_error(400, b"not json") == (400, b"not json")


def test_tokenize_routes_by_model(client, env):
    r = client.post("/tokenize", json={"model": "gpu-rig/coder-7b", "content": "a b c"})
    assert r.status_code == 200 and r.json()["tokens"] == [0, 1, 2]
    assert env["starts"]() == ["coder-7b"]


def test_busy_rig_returns_503(client, env, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "busy")
    r = chat(client)
    assert r.status_code == 503
    assert "rig busy: busy: bench" in r.json()["error"]["message"]


def test_no_capacity_returns_503(client, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "nocap")
    assert chat(client).status_code == 503


def test_failed_load_returns_502(client, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "fail")
    r = chat(client)
    assert r.status_code == 502 and "container exited" in r.json()["error"]["message"]


def test_locked_rig_is_retried(client, env, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "locked-once")
    assert chat(client).status_code == 200
    assert env["starts"]() == ["coder-7b", "coder-7b"]


def test_concurrent_cold_requests_share_one_start(env):
    rig = Rig(env["settings"].rig_command, 30)

    async def go():
        return await asyncio.gather(*(rig.ensure_ready("coder-7b") for _ in range(4)))

    assert asyncio.run(go()) == [UPSTREAM_PORT] * 4
    assert env["starts"]() == ["coder-7b"]


def test_start_failure_reaches_every_waiter(env, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "nocap")
    rig = Rig(env["settings"].rig_command, 30)

    async def go():
        return await asyncio.gather(*(rig.ensure_ready("coder-7b") for _ in range(3)), return_exceptions=True)

    results = asyncio.run(go())
    assert all(isinstance(r, RigUnavailable) and r.status == 503 for r in results)
    assert env["starts"]() == ["coder-7b"]


def test_usage_logged_from_timings(caplog):
    from llm_gateway.app import log_usage
    body = json.dumps({"choices": [], "timings": {"cache_n": 19000, "prompt_n": 412, "prompt_ms": 950,
                                                  "prompt_per_second": 433.7, "predicted_n": 20,
                                                  "predicted_per_second": 28.1}}).encode()
    stream_tail = b'data: {"choices":[]}\n\ndata: ' + body + b"\n\ndata: [DONE]\n\n"
    with caplog.at_level("INFO", logger="llm_gateway"):
        log_usage("chat-27b", 200, body)
        log_usage("chat-27b", 200, stream_tail)
        log_usage("chat-27b", 400, b'{"error":{}}')
    lines = [r.getMessage() for r in caplog.records]
    assert lines[0] == lines[1]
    assert "cached=19000 prompt=412 prompt_s=0.9" in lines[0]
    assert "(no timings)" in lines[2]


def test_unreachable_rig_returns_502(client, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_SSH", "down")
    r = chat(client)
    assert r.status_code == 502
    assert "rig unreachable over ssh" in r.json()["error"]["message"]


def test_start_past_deadline_is_not_killed(env, monkeypatch):
    """Over ssh, killing `rig start` could stop a load mid-way; the gateway only stops waiting."""
    monkeypatch.setenv("FAKE_RIG_START", "slow")
    settings = dataclasses.replace(env["settings"], start_timeout_seconds=1)
    app = create_app(settings, upstream_transport=httpx.MockTransport(fake_llama))
    with TestClient(app) as c:
        r = chat(c)
        assert r.status_code == 504 and "still loading" in r.json()["error"]["message"]
        time.sleep(2.5)
        assert chat(c).status_code == 200
    assert env["starts"]() == ["coder-7b"]


def test_status_shows_loads_in_progress(env, monkeypatch):
    monkeypatch.setenv("FAKE_RIG_START", "slow")
    rig = Rig(env["settings"].rig_command, 30)

    async def go():
        load = asyncio.create_task(rig.ensure_ready("coder-7b"))
        await asyncio.sleep(1.0)
        during = rig.loading()
        await load
        return during, rig.loading()

    during, after = asyncio.run(go())
    assert [x["id"] for x in during] == ["coder-7b"] and during[0]["elapsed_s"] >= 0.5
    assert after == []


def test_status_route_includes_gateway_loading(client):
    body = client.get("/v1/rig/status").json()
    assert body["mode"] == "idle" and body["gateway_loading"] == []


def test_ready_roster_uses_served_context_and_matching_identity(client, env):
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"mode": "interactive", "profiles": [
        {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 8192,
         "port": UPSTREAM_PORT, "state": "ready", "gpus": [2, 1]},
        {"id": "chat-27b", "model": "wrong-model", "ctx_size": 65536,
         "port": UPSTREAM_PORT, "state": "ready"},
        {"id": "unpublished", "model": "x", "ctx_size": 4096,
         "port": UPSTREAM_PORT, "state": "ready"},
    ]}))
    body = client.get("/v1/rig/ready-roster").json()
    assert body == {"object": "ready_roster", "version": 1, "data": [{
        "id": "gpu-rig/coder-7b", "model": "qwen2.5-coder:7b",
        "registry_profile": None, "state": "ready", "gpu_ids": [1, 2], "context_length": 8192,
        "output_limit": None, "tool_call_format": None, "tokenizer_path": "/tokenize",
    }]}
    assert env["starts"]() == []


def test_agent_type_resolves_disjoint_ready_route_without_loading(env):
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"mode": "interactive", "profiles": [
        {"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536,
         "port": UPSTREAM_PORT, "state": "ready", "gpus": [0]},
        {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 8192,
         "port": UPSTREAM_PORT, "state": "ready", "gpus": [2]},
    ]}))
    settings = dataclasses.replace(env["settings"], manager_enabled=True)
    app = create_app(settings, upstream_transport=httpx.MockTransport(fake_llama), run_reconciler=False)
    async def observed():
        return {"profiles": json.loads(state.read_text())["profiles"]}
    app.state.manager.status = observed
    with TestClient(app) as client:
        response = client.post("/v1/manager/agent-routes/resolve", json={
            "agent_type": "coding", "parent_route": "gpu-rig/chat-27b"})
    assert response.status_code == 200
    assert response.json()["route"] == "gpu-rig/coder-7b"
    assert response.json()["gpu_ids"] == [2]
    assert env["starts"]() == []


def test_agent_type_rejects_missing_separate_ready_route(env):
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"mode": "interactive", "profiles": [
        {"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536,
         "port": UPSTREAM_PORT, "state": "ready", "gpus": [0]},
        {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 8192,
         "port": UPSTREAM_PORT, "state": "ready", "gpus": [0]},
    ]}))
    settings = dataclasses.replace(env["settings"], manager_enabled=True)
    app = create_app(settings, upstream_transport=httpx.MockTransport(fake_llama), run_reconciler=False)
    async def observed():
        return {"profiles": json.loads(state.read_text())["profiles"]}
    app.state.manager.status = observed
    with TestClient(app) as client:
        response = client.post("/v1/manager/agent-routes/resolve", json={
            "agent_type": "coding", "parent_route": "gpu-rig/chat-27b"})
    assert response.status_code == 503
    assert env["starts"]() == []


def test_agent_type_prepares_cold_route_through_manager(env):
    settings = dataclasses.replace(env["settings"], manager_enabled=True)
    app = create_app(settings, upstream_transport=httpx.MockTransport(fake_llama), run_reconciler=False)
    rows = [{"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536,
             "port": UPSTREAM_PORT, "state": "ready", "gpus": [0]}]
    async def observed():
        return {"profiles": rows}
    async def load_profile(pid, spec):
        assert pid == "coder-7b" and spec["model"] == "qwen2.5-coder:7b"
        rows.append({"id": pid, "model": spec["model"], "ctx_size": 8192,
                     "port": UPSTREAM_PORT, "state": "ready", "gpus": [2]})
    async def ensure_ready(pid, start):
        await start()
        return UPSTREAM_PORT
    app.state.manager.status = observed
    app.state.manager.load_profile = load_profile
    app.state.manager.rig.ensure_ready = ensure_ready
    with TestClient(app) as client:
        response = client.post("/v1/manager/agent-routes/resolve", json={
            "agent_type": "coding", "parent_route": "gpu-rig/chat-27b"})
    assert response.status_code == 200
    assert response.json()["route"] == "gpu-rig/coder-7b"
    assert response.json()["gpu_ids"] == [2]


def test_ready_managed_strata_route_is_listed_and_rewrites_model(client, env):
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"mode": "bench", "profiles": [{
        "id": "q38-g0", "model": "qwen3.8-flash-next:q2_0", "backend": "strata",
        "upstream_model": "qwen3.8-flash-next-q2_0", "ctx_size": 8192,
        "port": UPSTREAM_PORT, "state": "ready",
    }]}))
    model = "gpu-rig/q38-g0"
    assert model in [m["id"] for m in client.get("/v1/models").json()["data"]]
    roster = client.get("/v1/rig/ready-roster").json()["data"]
    assert roster[-1]["id"] == model and roster[-1]["tokenizer_path"] is None
    response = chat(client, model=model)
    assert response.status_code == 200
    assert response.json()["echo_model"] == "qwen3.8-flash-next-q2_0"
    assert client.post("/tokenize", json={"model": model, "content": "hi"}).status_code == 501
    assert env["starts"]() == []


def test_ready_roster_excludes_cold_and_unknown_limits(client, env):
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"profiles": [
        {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 0,
         "port": UPSTREAM_PORT, "state": "ready"},
        {"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536,
         "port": UPSTREAM_PORT, "state": "loading"},
    ]}))
    assert client.get("/v1/rig/ready-roster").json()["data"] == []
    assert env["starts"]() == []


def test_ready_only_inference_and_tokenize_do_not_cold_load(client, env):
    headers = {"X-Require-Ready": "true"}
    assert client.post("/v1/chat/completions", headers=headers,
                       json={"model": "gpu-rig/coder-7b", "messages": []}).status_code == 409
    assert client.post("/tokenize", headers=headers,
                       json={"model": "gpu-rig/coder-7b", "content": "hi"}).status_code == 409
    assert env["starts"]() == []
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"profiles": [{"id": "coder-7b", "model": "wrong-model",
                                          "ctx_size": 8192, "state": "ready",
                                          "port": UPSTREAM_PORT}]}))
    assert client.post("/v1/chat/completions", headers=headers,
                       json={"model": "gpu-rig/coder-7b", "messages": []}).status_code == 409
    state.write_text(json.dumps({"profiles": [{"id": "coder-7b", "model": "qwen2.5-coder:7b",
                                          "ctx_size": 8192, "state": "ready",
                                          "port": UPSTREAM_PORT}]}))
    assert client.post("/v1/chat/completions", headers=headers,
                       json={"model": "gpu-rig/coder-7b", "messages": []}).status_code == 200
    assert client.post("/tokenize", headers=headers,
                       json={"model": "gpu-rig/coder-7b", "content": "hi"}).status_code == 200
    assert env["starts"]() == []


def test_ready_only_responses_routes_without_cold_load(client, env):
    request = {"model": "gpu-rig/coder-7b", "input": "Say ready", "stream": False}
    headers = {"X-Require-Ready": "true"}
    cold = client.post("/v1/responses", headers=headers, json=request)
    assert cold.status_code == 409 and cold.json()["error"]["type"] == "model_not_ready"
    assert env["starts"]() == []
    state = Path(os.environ["FAKE_RIG_STATE"])
    state.write_text(json.dumps({"profiles": [{"id": "coder-7b", "model": "qwen2.5-coder:7b",
                                          "ctx_size": 8192, "state": "ready", "port": UPSTREAM_PORT}]}))
    warm = client.post("/v1/responses", headers=headers, json=request)
    assert warm.status_code == 200 and warm.json()["echo_model"] == request["model"]
    assert env["starts"]() == []


@pytest.fixture
def keyed_client(env):
    settings = dataclasses.replace(env["settings"], api_keys=("k-one", "k-two"))
    app = create_app(settings, upstream_transport=httpx.MockTransport(fake_llama))
    with TestClient(app) as c:
        yield c


def test_api_keys_required_when_configured(keyed_client):
    assert keyed_client.get("/v1/models").status_code == 401
    assert keyed_client.get("/v1/rig/ready-roster").status_code == 401
    r = keyed_client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"
    assert keyed_client.get("/v1/models", headers={"Authorization": "Bearer k-two"}).status_code == 200
    assert keyed_client.get("/health").status_code == 200


def test_no_keys_means_no_auth(client):
    assert client.get("/v1/models", headers={"Authorization": "Bearer anything"}).status_code == 200


def test_lan_bind_requires_keys(env, tmp_path):
    lan = dataclasses.replace(env["settings"], bind="10.0.0.2")
    with pytest.raises(RuntimeError, match="not loopback"):
        lan.validate()
    dataclasses.replace(lan, api_keys=("k",)).validate()
    keys = tmp_path / "keys"
    keys.write_text("# comment\n\nk-one\n  k-two  \n")
    assert read_api_keys(keys) == ("k-one", "k-two")
    assert read_api_keys(tmp_path / "missing") == ()


def test_rig_arguments_survive_ssh():
    """ssh hands the remote shell one string; spaces and JSON must arrive as single arguments."""
    import shlex
    spec = '{"controller": "interactive", "gpu_ids": [0]}'
    over_ssh = Rig(("/usr/bin/ssh", "-o", "BatchMode=yes", "10.0.0.3", "/usr/local/bin/rig"), 30)
    remote_line = " ".join(over_ssh.argv("lease", "acquire", "--spec", spec)[5:])
    assert shlex.split(remote_line) == ["lease", "acquire", "--spec", spec]
    local = Rig(("rig",), 30)
    assert local.argv("claim", "add", "x", "--note", "two words") == ["rig", "claim", "add", "x", "--note", "two words"]
    windows = Rig((r"C:\WINDOWS\System32\OpenSSH\ssh.exe", "rig", "/usr/local/bin/rig"), 30)
    assert windows.argv("claim", "add", "x", "--note", "two words")[-1] == "'two words'"
