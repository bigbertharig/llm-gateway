from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .manager import Manager, ManagerError
from .manager.routes import router as manager_router
from .dashboard import router as dashboard_router
from .dashboard.chat import router as chat_router
from .dashboard.codex_bridge import CodexBridge
from .profiles import ProfileError, Profiles
from .rig import Rig, RigUnavailable
from .settings import Settings

log = logging.getLogger("llm_gateway")


class GatewayError(Exception):
    def __init__(self, status: int, message: str, kind: str = "gateway_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def error_response(status: int, message: str, kind: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "code": status, **extra}}, status)


def translate_upstream_error(status: int, content: bytes) -> tuple[int, bytes]:
    """Map llama-server's context overflow onto OpenAI's shape.

    Harnesses (Roo, DeepSeek Harness) only compact-and-retry on overflow when they recognize the
    error, and they look for OpenAI's `context_length_exceeded` code and wording. Every other
    upstream error passes through unchanged.
    """
    if status != 400:
        return status, content
    try:
        err = json.loads(content)["error"]
    except (ValueError, KeyError, TypeError):
        return status, content
    if not isinstance(err, dict) or err.get("type") != "exceed_context_size_error":
        return status, content
    n_ctx, n_prompt = err.get("n_ctx"), err.get("n_prompt_tokens")
    payload = {"error": {
        "message": (f"This model's maximum context length is {n_ctx} tokens. However, your messages "
                    f"resulted in {n_prompt} tokens. Please reduce the length of the messages."),
        "type": "invalid_request_error",
        "param": "messages",
        "code": "context_length_exceeded",
        "n_ctx": n_ctx,
        "n_prompt_tokens": n_prompt,
    }}
    return 400, json.dumps(payload).encode()


def log_usage(profile: str, status: int, payload: bytes) -> None:
    """One line per inference request: prompt size, cache reuse, speed (from llama `timings`).

    `cached` is the prompt prefix llama-server reused from its cache; `prompt` is what it actually
    had to process. Watching these while changing client settings shows what each setting costs.
    """
    t: dict = {}
    for chunk in reversed(payload.split(b"\n")):
        line = chunk.strip().removeprefix(b"data:").strip()
        if b'"timings"' not in line:
            continue
        try:
            t = json.loads(line).get("timings") or {}
        except ValueError:
            pass
        break
    if not t:
        log.info("usage profile=%s status=%s (no timings)", profile, status)
        return
    log.info(
        "usage profile=%s status=%s cached=%s prompt=%s prompt_s=%.1f prompt_tps=%.0f gen=%s gen_tps=%.1f",
        profile, status, t.get("cache_n"), t.get("prompt_n"), (t.get("prompt_ms") or 0) / 1000,
        t.get("prompt_per_second") or 0, t.get("predicted_n"), t.get("predicted_per_second") or 0,
    )


def create_app(
    settings: Settings | None = None,
    *,
    rig: Rig | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    run_reconciler: bool = True,
) -> FastAPI:
    s = settings or Settings.from_env()
    profiles = Profiles(s.profiles_path)
    rig = rig or Rig(s.rig_command, s.start_timeout_seconds)
    manager: Manager | None = None
    codex_bridge = CodexBridge()
    if s.manager_enabled:
        manager = Manager(rig, s.start_timeout_seconds, s.controller_id, mirror_path=s.mirror_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s.validate()
        timeout = httpx.Timeout(s.request_timeout_seconds, connect=10.0)
        app.state.http = httpx.AsyncClient(timeout=timeout, transport=upstream_transport)
        reconciler = asyncio.create_task(manager.run_reconciler()) if manager and run_reconciler else None
        yield
        if reconciler:
            reconciler.cancel()
        await codex_bridge.close()
        await app.state.http.aclose()

    app = FastAPI(title="llm-gateway", version="0.4.0", lifespan=lifespan)
    app.state.manager = manager
    if manager is not None:
        app.include_router(manager_router(manager, profiles.all, s.model_prefix))
        app.include_router(dashboard_router(manager))

    @app.exception_handler(ManagerError)
    async def _manager_error(_req, exc: ManagerError):
        return error_response(exc.status, str(exc), "manager_error", **exc.extra)

    def start_for(pid: str):
        """Cold-load path: through the manager when it runs, else plain `rig start remote`."""
        if manager is None:
            return None
        return lambda: manager.load_profile(pid, profiles.all()[pid])

    @app.exception_handler(GatewayError)
    async def _gateway_error(_req, exc: GatewayError):
        return error_response(exc.status, str(exc), exc.kind)

    @app.exception_handler(RigUnavailable)
    async def _rig_error(_req, exc: RigUnavailable):
        return error_response(exc.status, str(exc), "rig_unavailable")

    @app.exception_handler(ProfileError)
    async def _profile_error(_req, exc: ProfileError):
        return error_response(500, str(exc), "profiles_error")

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        """Bearer keys, when configured. /health stays open for liveness checks."""
        if s.api_keys and request.url.path != "/health":
            header = request.headers.get("authorization", "")
            token = header[7:].strip() if header[:7].lower() == "bearer " else ""
            if not any(hmac.compare_digest(token.encode(), k.encode()) for k in s.api_keys):
                return error_response(401, "invalid or missing API key", "invalid_request_error",
                                      code="invalid_api_key")
        return await call_next(request)

    def resolve_profile(model: object) -> str:
        prefix = s.model_prefix + "/"
        published = profiles.all()
        names = sorted(prefix + pid for pid in published)
        if not isinstance(model, str) or not model.startswith(prefix):
            raise GatewayError(404, f"model must be one of {names}", "model_not_found")
        pid = model[len(prefix):]
        return pid  # a ready managed Strata load can also be addressed by its profile id

    async def read_json(request: Request) -> tuple[bytes, dict]:
        raw = await request.body()
        try:
            body = json.loads(raw or b"null")
        except ValueError as exc:
            raise GatewayError(400, f"invalid JSON body: {exc}", "invalid_request") from exc
        if not isinstance(body, dict):
            raise GatewayError(400, "body must be a JSON object", "invalid_request")
        return raw, body

    async def forward(request: Request, path: str, raw: bytes, port: int, stream: bool,
                      profile: str | None = None) -> Response:
        """Send the body unchanged (llama-server ignores `model`) and relay the reply.

        With `profile` set, logs the request's token usage (see log_usage).
        """
        client: httpx.AsyncClient = request.app.state.http
        upstream_req = client.build_request(
            "POST", f"http://{s.upstream_host}:{port}{path}",
            content=raw, headers={"Content-Type": "application/json"},
        )
        try:
            resp = await client.send(upstream_req, stream=True)
        except httpx.RequestError as exc:
            raise GatewayError(502, f"model endpoint :{port} unavailable: {exc}", "upstream_error") from exc
        ctype = resp.headers.get("content-type", "application/json")
        if not stream or resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            if profile:
                log_usage(profile, resp.status_code, content)
            status, content = translate_upstream_error(resp.status_code, content)
            return Response(content, status, media_type=ctype.split(";")[0])

        async def relay():
            # Starlette cancels this on client disconnect; closing the upstream stream then
            # makes llama-server stop generating.
            tail = b""
            try:
                async for chunk in resp.aiter_raw():
                    tail = (tail + chunk)[-8192:]  # llama puts `timings` in the final chunk
                    yield chunk
            finally:
                await resp.aclose()
                if profile:
                    log_usage(profile, resp.status_code, tail)

        return StreamingResponse(relay(), resp.status_code, media_type=ctype.split(";")[0],
                                 headers={"Cache-Control": "no-cache"})

    if manager is not None:
        app.include_router(chat_router(manager, profiles, forward, codex_bridge))

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    def shared_ready(status: dict) -> list[dict]:
        service = status.get("shared_kv_service") or {}
        if service.get("status") != "ready" or service.get("model") != "qwen3.6:35b-a3b-xl":
            return []
        rows = []
        for chat in service.get("chats") or []:
            if not isinstance(chat, dict) or not isinstance(chat.get("chat_id"), str):
                continue
            gpu, cap = chat.get("gpu"), chat.get("context_cap")
            if type(gpu) is int and gpu in range(6) and type(cap) is int and cap > 0:
                rows.append({"id": f"{s.model_prefix}/shared-35b-{chat['chat_id']}",
                             "model": service["model"], "state": "ready",
                             "gpu_ids": [gpu], "context_length": cap,
                             "used_tokens": chat.get("used_tokens", 0)})
        return rows

    async def shared_completion(pid: str, body: dict) -> Response:
        route = f"{s.model_prefix}/{pid}"
        ready = next((row for row in shared_ready(await rig.status()) if row["id"] == route), None)
        if not ready:
            raise GatewayError(409, f"model {route} is not ready", "model_not_ready")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
            raise GatewayError(400, "messages must be a nonempty list", "invalid_request")
        def message_text(value):
            if isinstance(value, str):
                return value
            if isinstance(value, list) and all(isinstance(item, dict) and
                    item.get("type") == "text" and isinstance(item.get("text"), str) for item in value):
                return "\n".join(item["text"] for item in value)
            raise GatewayError(400, "shared chat supports text messages only", "invalid_request")
        latest = messages[-1]
        if latest.get("role") not in ("user", "tool"):
            raise GatewayError(400, "shared chat expects a user or tool message last", "invalid_request")
        latest_text = message_text(latest.get("content"))
        tools = body.get("tools") or []
        if not isinstance(tools, list):
            raise GatewayError(400, "tools must be a list", "invalid_request")
        allowed = {}
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict):
                fn = tool["function"]
                if isinstance(fn.get("name"), str):
                    allowed[fn["name"]] = fn
        prompt = latest_text
        if ready["used_tokens"] == 0:
            system = "\n".join(message_text(m.get("content")) for m in messages if m.get("role") == "system")
            if system:
                prompt = "System instructions:\n" + system + "\n\nUser request:\n" + prompt
        if allowed and latest["role"] == "user":
            advertised = [{"name": name, "description": fn.get("description", ""),
                           "parameters": fn.get("parameters", {})} for name, fn in allowed.items()]
            prompt += ("\n\nAvailable tools: " + json.dumps(advertised) +
                       "\nIf a tool is needed, return exactly one JSON object with keys name and arguments. "
                       "Use an advertised tool name and valid arguments. Otherwise answer normally.")
        elif latest["role"] == "tool":
            prompt = ("Tool result for " + str(latest.get("tool_call_id") or "previous call") + ":\n" + prompt +
                      "\nContinue the original request using this result. Answer directly.")
        limit = body.get("max_tokens", 256)
        if type(limit) is not int or limit < 1:
            raise GatewayError(400, "max_tokens must be positive", "invalid_request")
        limit = min(limit, 512)
        result = await rig.shared_kv("turn", chat_id=pid.removeprefix("shared-35b-"),
                                     message=prompt, max_tokens=limit)
        content = result.get("content")
        if not isinstance(content, str):
            raise GatewayError(502, "shared chat returned no content", "upstream_error")
        message = {"role": "assistant", "content": content}
        finish = result.get("finish_reason", "stop")
        total_generated = result.get("generated_tokens", 0)
        if allowed and latest["role"] == "user":
            def strict_proposal(value):
                candidate = value.strip()
                fenced = re.fullmatch(r"```(?:json)?\s*\n(\{[\s\S]*\})\s*\n```", candidate)
                if fenced:
                    candidate = fenced.group(1)
                try:
                    proposed = json.loads(candidate)
                except ValueError:
                    return None
                if isinstance(proposed, dict) and proposed.get("name") == "tool_call":
                    wrapper = proposed.get("arguments")
                    calls = wrapper.get("calls") if isinstance(wrapper, dict) else None
                    proposed = calls[0] if isinstance(calls, list) and len(calls) == 1 else None
                return proposed if (isinstance(proposed, dict) and proposed.get("name") in allowed
                                    and isinstance(proposed.get("arguments"), dict)) else None
            proposed = strict_proposal(content)
            if proposed is None and '"name"' in content and any(name in content for name in allowed):
                log.info("shared tool format repair route=%s", route)
                repair = await rig.shared_kv("turn", chat_id=pid.removeprefix("shared-35b-"),
                    message=("Your previous reply attempted a tool call but was not a valid call. "
                             "If you intend to call a tool, reply with exactly one raw JSON object "
                             "with name and arguments using the advertised schema. "
                             "No prose, markdown fence, or explanation."), max_tokens=min(limit, 128))
                repaired_content = repair.get("content")
                if isinstance(repaired_content, str):
                    content = repaired_content
                    result = repair
                    total_generated += repair.get("generated_tokens", 0)
                    proposed = strict_proposal(content)
                    message = {"role": "assistant", "content": content}
                    finish = repair.get("finish_reason", "stop")
            if proposed is not None:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_" + uuid.uuid4().hex, "type": "function",
                    "function": {"name": proposed["name"],
                                 "arguments": json.dumps(proposed["arguments"], separators=(",", ":"))}}]}
                finish = "tool_calls"
        generated = total_generated
        used = result.get("used_tokens", 0)
        usage = {"prompt_tokens": max(0, used-ready["used_tokens"]-generated),
                 "completion_tokens": generated,
                 "total_tokens": max(0, used-ready["used_tokens"])}
        completion_id = "chatcmpl-" + uuid.uuid4().hex
        created = int(time.time())
        if body.get("stream"):
            async def chunks():
                def event(delta: dict, reason=None, *, usage_row=None):
                    payload = {"id": completion_id, "object": "chat.completion.chunk",
                               "created": created, "model": route,
                               "choices": [] if usage_row is not None else [
                                   {"index": 0, "delta": delta, "finish_reason": reason}]}
                    if usage_row is not None:
                        payload["usage"] = usage_row
                    return ("data: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()
                yield event({"role": "assistant"})
                if "tool_calls" in message:
                    yield event({"tool_calls": [{"index": 0, **message["tool_calls"][0]}]})
                elif content:
                    yield event({"content": content})
                yield event({}, finish)
                if (body.get("stream_options") or {}).get("include_usage"):
                    yield event({}, usage_row=usage)
                yield b"data: [DONE]\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache"})
        return JSONResponse({"id": completion_id, "object": "chat.completion",
                             "created": created, "model": route,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                             "usage": usage})

    async def shared_responses(pid: str, body: dict) -> Response:
        """Responses API envelope for the same ready, resident shared chat."""
        request_input = body.get("input")
        if isinstance(request_input, str):
            last_role, last_text = "user", request_input
        elif isinstance(request_input, list) and request_input:
            last = request_input[-1]
            if not isinstance(last, dict):
                raise GatewayError(400, "shared Responses input must contain message objects", "invalid_request")
            if last.get("type") == "function_call_output":
                last_role, last_text = "tool", last.get("output")
            else:
                last_role, last_text = last.get("role"), last.get("content")
        else:
            raise GatewayError(400, "shared Responses input is required", "invalid_request")
        if last_role not in ("user", "tool"):
            raise GatewayError(400, "shared Responses expects a user or tool input last", "invalid_request")
        if isinstance(last_text, list):
            parts = []
            for part in last_text:
                if not isinstance(part, dict) or part.get("type") not in ("input_text", "text") \
                        or not isinstance(part.get("text"), str):
                    raise GatewayError(400, "shared Responses supports text input only", "invalid_request")
                parts.append(part["text"])
            last_text = "\n".join(parts)
        if not isinstance(last_text, str):
            raise GatewayError(400, "shared Responses supports text input only", "invalid_request")
        instruction = body.get("instructions")
        messages = ([{"role": "system", "content": instruction}] if isinstance(instruction, str) else [])
        messages.append({"role": last_role, "content": last_text})
        tools = []
        for item in body.get("tools") or []:
            if isinstance(item, dict) and item.get("type") == "function" and isinstance(item.get("name"), str):
                tools.append({"type": "function", "function": {"name": item["name"],
                              "description": item.get("description", ""),
                              "parameters": item.get("parameters", {})}})
        forwarded = {"model": f"{s.model_prefix}/{pid}", "messages": messages, "tools": tools,
                     "max_tokens": body.get("max_output_tokens") or 256, "stream": False}
        completion = json.loads((await shared_completion(pid, forwarded)).body)
        answer = completion["choices"][0]["message"]
        if answer.get("tool_calls"):
            call = answer["tool_calls"][0]
            item = {"id": "fc_" + uuid.uuid4().hex, "type": "function_call", "status": "completed",
                    "call_id": call["id"], "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"]}
        else:
            item = {"id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "completed",
                    "role": "assistant", "content": [{"type": "output_text",
                    "text": answer.get("content") or "", "annotations": []}]}
        usage = completion["usage"]
        response = {"id": "resp_" + uuid.uuid4().hex, "object": "response",
                    "created_at": int(time.time()), "status": "completed", "model": forwarded["model"],
                    "output": [item], "error": None, "incomplete_details": None,
                    "usage": {"input_tokens": usage["prompt_tokens"],
                              "output_tokens": usage["completion_tokens"],
                              "total_tokens": usage["total_tokens"],
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}},
                    "parallel_tool_calls": False, "tool_choice": body.get("tool_choice", "auto"),
                    "tools": body.get("tools") or [], "store": False}
        if not body.get("stream"):
            return JSONResponse(response)
        async def events():
            sequence = 0
            def event(kind, **fields):
                nonlocal sequence
                sequence += 1
                payload = {"type": kind, "sequence_number": sequence, **fields}
                return ("event: " + kind + "\ndata: " + json.dumps(payload, separators=(",", ":")) + "\n\n").encode()
            yield event("response.created", response={**response, "status": "in_progress", "output": [], "usage": None})
            yield event("response.output_item.added", output_index=0,
                        item={**item, "status": "in_progress",
                              **({"arguments": ""} if item["type"] == "function_call" else {"content": []})})
            if item["type"] == "function_call":
                yield event("response.function_call_arguments.delta", item_id=item["id"], output_index=0,
                            delta=item["arguments"])
                yield event("response.function_call_arguments.done", item_id=item["id"], output_index=0,
                            arguments=item["arguments"])
            else:
                part = {"type": "output_text", "text": "", "annotations": []}
                yield event("response.content_part.added", item_id=item["id"], output_index=0,
                            content_index=0, part=part)
                yield event("response.output_text.delta", item_id=item["id"], output_index=0,
                            content_index=0, delta=item["content"][0]["text"])
                yield event("response.output_text.done", item_id=item["id"], output_index=0,
                            content_index=0, text=item["content"][0]["text"])
                yield event("response.content_part.done", item_id=item["id"], output_index=0,
                            content_index=0, part=item["content"][0])
            yield event("response.output_item.done", output_index=0, item=item)
            yield event("response.completed", response=response)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/v1/models")
    async def models():
        data = [
            {
                "id": f"{s.model_prefix}/{pid}",
                "object": "model",
                "created": 0,
                "owned_by": s.model_prefix,
                "description": p.get("description", ""),
                "context_length": p.get("ctx_size"),
            }
            for pid, p in profiles.all().items()
        ]
        try:
            status = await rig.status()
        except RigUnavailable:
            status = {}
        for loaded in dynamic_strata_ready(status):
            data.append({"id": f"{s.model_prefix}/{loaded['id']}", "object": "model",
                         "created": 0, "owned_by": s.model_prefix,
                         "description": f"managed Strata load: {loaded['model']}",
                         "context_length": loaded["ctx_size"]})
        for row in shared_ready(status):
            data.append({"id": row["id"], "object": "model", "created": 0,
                         "owned_by": s.model_prefix, "description": "35B shared-weight chat",
                         "context_length": row["context_length"]})
        return {"object": "list", "data": data}

    @app.get("/v1/rig/status")
    async def rig_status():
        """`rig status --json`, plus the loads this gateway is waiting on and for how long."""
        return {**await rig.status(), "gateway_loading": rig.loading()}

    def matching_ready(status: dict):
        """Only published, identity-matched routes with a known served context and port."""
        offered = profiles.all()
        for loaded in status.get("profiles", []):
            if not isinstance(loaded, dict) or loaded.get("state") != "ready":
                continue
            pid = loaded.get("id")
            profile = offered.get(pid) if isinstance(pid, str) else None
            if not profile or loaded.get("model") != profile.get("model"):
                continue
            ctx = loaded.get("ctx_size")
            port = loaded.get("port")
            if not isinstance(ctx, int) or ctx <= 0 or not isinstance(port, int) or port <= 0:
                continue
            yield pid, profile, loaded

    def dynamic_strata_ready(status: dict):
        offered = profiles.all()
        for loaded in status.get("profiles", []):
            if not isinstance(loaded, dict) or loaded.get("backend") != "strata" or loaded.get("state") != "ready":
                continue
            if loaded.get("id") in offered or not isinstance(loaded.get("id"), str):
                continue
            if not isinstance(loaded.get("model"), str) or not isinstance(loaded.get("upstream_model"), str):
                continue
            if not isinstance(loaded.get("ctx_size"), int) or loaded["ctx_size"] <= 0:
                continue
            if not isinstance(loaded.get("port"), int) or loaded["port"] <= 0:
                continue
            yield loaded

    @app.get("/v1/rig/ready-roster")
    async def ready_roster():
        """Read-only routes a harness may use without asking the rig to load a model."""
        data = []
        status = await rig.status()
        for pid, profile, loaded in matching_ready(status):
            data.append({
                "id": f"{s.model_prefix}/{pid}",
                "model": loaded["model"],
                "registry_profile": profile.get("registry_profile"),
                "state": "ready",
                "gpu_ids": sorted(loaded.get("gpus") or []),
                "context_length": loaded["ctx_size"],
                "output_limit": None,
                "tool_call_format": None,
                "tokenizer_path": "/tokenize",
            })
        for loaded in dynamic_strata_ready(status):
            data.append({"id": f"{s.model_prefix}/{loaded['id']}", "model": loaded["model"],
                         "registry_profile": None, "state": "ready",
                         "gpu_ids": sorted(loaded.get("gpus") or []),
                         "context_length": loaded["ctx_size"], "output_limit": None,
                         "tool_call_format": None, "tokenizer_path": None})
        for row in shared_ready(status):
            data.append({"id": row["id"], "model": row["model"], "registry_profile": None,
                         "state": "ready", "gpu_ids": row["gpu_ids"],
                         "context_length": row["context_length"], "output_limit": 512,
                         "tool_call_format": "strict_json_bridge", "tokenizer_path": None})
        return {"object": "ready_roster", "version": 1, "data": data}

    async def dynamic_strata_route(pid: str) -> dict:
        for loaded in dynamic_strata_ready(await rig.status()):
            if loaded["id"] == pid:
                return loaded
        names = sorted(f"{s.model_prefix}/{name}" for name in profiles.all())
        raise GatewayError(404, f"unknown model {s.model_prefix}/{pid}; published: {names}; "
                           "managed Strata routes must already be ready", "model_not_found")

    async def route_port(request: Request, pid: str) -> int:
        if request.headers.get("X-Require-Ready", "").lower() == "true":
            for ready_id, _profile, loaded in matching_ready(await rig.status()):
                if ready_id == pid:
                    return loaded["port"]
            raise GatewayError(409, f"model {s.model_prefix}/{pid} is not ready", "model_not_ready")
        return await rig.ensure_ready(pid, start_for(pid))

    @app.post("/v1/chat/completions")
    @app.post("/v1/completions")
    @app.post("/v1/responses")
    async def completions(request: Request):
        raw, body = await read_json(request)
        pid = resolve_profile(body.get("model"))
        if pid.startswith("shared-35b-"):
            if request.url.path == "/v1/chat/completions":
                return await shared_completion(pid, body)
            if request.url.path == "/v1/responses":
                return await shared_responses(pid, body)
            raise GatewayError(501, "shared chats support Chat Completions and Responses", "unsupported")
        if pid in profiles.all():
            port = await route_port(request, pid)
        else:
            loaded = await dynamic_strata_route(pid)
            port = loaded["port"]
            raw = json.dumps({**body, "model": loaded["upstream_model"]}).encode()
        return await forward(request, request.url.path, raw, port, stream=bool(body.get("stream")), profile=pid)

    @app.post("/tokenize")
    @app.post("/detokenize")
    async def tokenize(request: Request):
        """llama-server tokenizer, routed by `model`. Exact token counts for compaction decisions."""
        raw, body = await read_json(request)
        pid = resolve_profile(body.get("model"))
        if pid not in profiles.all():
            raise GatewayError(501, "Strata does not expose the gateway tokenizer endpoint", "unsupported")
        port = await route_port(request, pid)
        return await forward(request, request.url.path, raw, port, stream=False)

    return app
