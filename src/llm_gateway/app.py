from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

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


def create_app(
    settings: Settings | None = None,
    *,
    rig: Rig | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    s = settings or Settings.from_env()
    profiles = Profiles(s.profiles_path)
    rig = rig or Rig(s.rig_command, s.start_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s.validate()
        timeout = httpx.Timeout(s.request_timeout_seconds, connect=10.0)
        app.state.http = httpx.AsyncClient(timeout=timeout, transport=upstream_transport)
        yield
        await app.state.http.aclose()

    app = FastAPI(title="llm-gateway", version="0.3.0", lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def _gateway_error(_req, exc: GatewayError):
        return error_response(exc.status, str(exc), exc.kind)

    @app.exception_handler(RigUnavailable)
    async def _rig_error(_req, exc: RigUnavailable):
        return error_response(exc.status, str(exc), "rig_unavailable")

    @app.exception_handler(ProfileError)
    async def _profile_error(_req, exc: ProfileError):
        return error_response(500, str(exc), "profiles_error")

    def resolve_profile(model: object) -> str:
        prefix = s.model_prefix + "/"
        published = profiles.all()
        names = sorted(prefix + pid for pid in published)
        if not isinstance(model, str) or not model.startswith(prefix):
            raise GatewayError(404, f"model must be one of {names}", "model_not_found")
        pid = model[len(prefix):]
        if pid not in published:
            raise GatewayError(404, f"unknown model {model!r}; published: {names}", "model_not_found")
        return pid

    async def read_json(request: Request) -> tuple[bytes, dict]:
        raw = await request.body()
        try:
            body = json.loads(raw or b"null")
        except ValueError as exc:
            raise GatewayError(400, f"invalid JSON body: {exc}", "invalid_request") from exc
        if not isinstance(body, dict):
            raise GatewayError(400, "body must be a JSON object", "invalid_request")
        return raw, body

    async def forward(request: Request, path: str, raw: bytes, port: int, stream: bool) -> Response:
        """Send the body unchanged (llama-server ignores `model`) and relay the reply."""
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
            status, content = translate_upstream_error(resp.status_code, content)
            return Response(content, status, media_type=ctype.split(";")[0])

        async def relay():
            # Starlette cancels this on client disconnect; closing the upstream stream then
            # makes llama-server stop generating.
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(relay(), resp.status_code, media_type=ctype.split(";")[0],
                                 headers={"Cache-Control": "no-cache"})

    @app.get("/health")
    async def health():
        return {"status": "ok"}

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
        return {"object": "list", "data": data}

    @app.get("/v1/rig/status")
    async def rig_status():
        return await rig.status()

    @app.post("/v1/chat/completions")
    @app.post("/v1/completions")
    async def completions(request: Request):
        raw, body = await read_json(request)
        port = await rig.ensure_ready(resolve_profile(body.get("model")))
        return await forward(request, request.url.path, raw, port, stream=bool(body.get("stream")))

    @app.post("/tokenize")
    @app.post("/detokenize")
    async def tokenize(request: Request):
        """llama-server tokenizer, routed by `model`. Exact token counts for compaction decisions."""
        raw, body = await read_json(request)
        port = await rig.ensure_ready(resolve_profile(body.get("model")))
        return await forward(request, request.url.path, raw, port, stream=False)

    return app
