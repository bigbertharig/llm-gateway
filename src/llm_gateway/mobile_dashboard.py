"""Private Tailscale Serve front door for the mobile dashboard.

This is a separate process from the LAN certificate front door. It forwards only
named operations to the loopback gateway, and each operation has its own mobile
permission. Tailscale Serve must be the only caller of this loopback listener.
"""

from __future__ import annotations

import os
import re
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse


UPSTREAM = "http://127.0.0.1:8080"
STATIC = Path(__file__).parent / "dashboard" / "static" / "mobile.html"
MAX_BODY = 1_048_576
MAX_TURN_BODY = 15 * 1024 * 1024
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    ),
}


def operation(method: str, path: str) -> tuple[str, str] | None:
    """Return (permission, upstream path) for an exact mobile operation."""
    if method == "POST" and path == "/mobile/api/conversations":
        return "create", "/dashboard/api/chat/conversations"
    if method in ("GET", "HEAD"):
        static = {
            "/mobile/api/rig": ("observe", "/dashboard/api/rig"),
            "/mobile/api/models": ("observe", "/dashboard/api/models"),
            "/mobile/api/conversations": ("observe", "/dashboard/api/chat/conversations"),
            "/mobile/api/options": ("observe", "/dashboard/api/chat/options"),
        }
        if path in static:
            return static[path]
    match = re.fullmatch(r"/mobile/api/conversations/([0-9a-f]{32})(?:/(.*))?", path)
    if match:
        cid, suffix = match.groups()
        base = f"/dashboard/api/chat/conversations/{cid}"
        if method in ("GET", "HEAD") and suffix in (None, "agent"):
            return "observe", base + ("/" + suffix if suffix else "")
        if method in ("GET", "HEAD") and suffix in (
                "workspace/tree", "workspace/file", "workspace/image", "workspace/search"):
            return "workspace-read", base + "/" + suffix
        if method == "POST" and suffix in ("turn", "agent/steer"):
            return "continue", base + "/" + suffix
        if method == "POST" and suffix in ("prepare", "park", "model"):
            return "model-control", base + "/" + suffix
        if method == "POST" and suffix == "agent/interrupt":
            return "interrupt", base + "/" + suffix
        if method == "POST" and suffix == "agent/command":
            return "agent-command", base + "/" + suffix
        if method == "POST" and suffix == "agent/approval":
            return "approve", base + "/" + suffix
        if method == "POST" and suffix == "workspace/file":
            return "workspace-write", base + "/" + suffix
    if method == "POST":
        controls = {f"/mobile/api/rig/{name}": ("rig-control", f"/dashboard/api/rig/{name}")
                    for name in ("kill", "unload", "unclaim", "cancel", "load")}
        controls.update({f"/mobile/api/rig/run/{name}":
                         ("rig-control", f"/dashboard/api/rig/run/{name}")
                         for name in ("recover", "dismiss", "stop")})
        return controls.get(path)
    return None


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(base_url=UPSTREAM, transport=transport, trust_env=False,
                                     follow_redirects=False, timeout=35) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.api_route("/mobile/{path:path}", methods=["GET", "HEAD", "POST"])
    async def mobile(path: str, request: Request):
        if b"%" in request.scope.get("raw_path", b""):
            return JSONResponse({"error": "not found"}, status_code=404, headers=HEADERS)
        user = os.getenv("MOBILE_TAILSCALE_USER", "").strip().lower()
        host = os.getenv("MOBILE_TAILSCALE_HOST", "").strip().lower()
        proxy_token = os.getenv("MOBILE_PROXY_TOKEN", "")
        if not user or not host or len(proxy_token) < 32:
            return JSONResponse({"error": "mobile access is not configured"}, status_code=503, headers=HEADERS)
        if request.headers.get("tailscale-user-login", "").strip().lower() != user:
            return JSONResponse({"error": "forbidden"}, status_code=403, headers=HEADERS)
        if request.headers.get("host", "").lower() != host:
            return JSONResponse({"error": "invalid host"}, status_code=400, headers=HEADERS)
        route = request.url.path
        if route in ("/mobile", "/mobile/") and request.method in ("GET", "HEAD"):
            return HTMLResponse(STATIC.read_text(encoding="utf-8"), headers=HEADERS)
        if route == "/mobile/api/capabilities" and request.method == "GET":
            enabled = sorted({item.strip() for item in os.getenv(
            "MOBILE_PERMISSIONS", "observe,create,continue,interrupt,approve,workspace-read").split(",") if item.strip()})
            return JSONResponse({"permissions": enabled}, headers=HEADERS)
        selected = operation(request.method, route)
        if selected is None:
            return JSONResponse({"error": "not found"}, status_code=404, headers=HEADERS)
        permission, upstream = selected
        enabled = {item.strip() for item in os.getenv(
            "MOBILE_PERMISSIONS", "observe,create,continue,interrupt,approve,workspace-read").split(",")}
        if permission not in enabled:
            return JSONResponse({"error": "mobile permission denied", "permission": permission},
                                status_code=403, headers=HEADERS)
        body = b""
        if request.method == "POST":
            if request.headers.get("origin", "").lower() != f"https://{host}":
                return JSONResponse({"error": "invalid origin"}, status_code=403, headers=HEADERS)
            if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
                return JSONResponse({"error": "cross-site request"}, status_code=403, headers=HEADERS)
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"error": "JSON required"}, status_code=415, headers=HEADERS)
            limit = MAX_TURN_BODY if upstream.endswith("/turn") else MAX_BODY
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "invalid length"}, status_code=400, headers=HEADERS)
            if length < 0 or length > limit:
                return JSONResponse({"error": "request too large"}, status_code=413, headers=HEADERS)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > limit:
                    return JSONResponse({"error": "request too large"}, status_code=413, headers=HEADERS)
            body = bytes(chunks)
        try:
            streaming = (upstream.endswith("/turn") and body and
                         json.loads(body).get("stream") is True)
        except (ValueError, AttributeError):
            streaming = False
        try:
            if streaming:
                outgoing = request.app.state.client.build_request(
                    request.method, upstream, params=request.url.query, content=body,
                    headers={"X-Mobile-Proxy-Token": proxy_token,
                             "content-type": "application/json"}, timeout=3600)
                response = await request.app.state.client.send(outgoing, stream=True)
                async def relay():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    finally:
                        await response.aclose()
                headers = dict(HEADERS)
                headers["Cache-Control"] = "no-cache"
                headers["X-Accel-Buffering"] = "no"
                return StreamingResponse(relay(), status_code=response.status_code,
                                         media_type=response.headers.get("content-type", "text/event-stream"),
                                         headers=headers)
            response = await request.app.state.client.request(
                request.method, upstream, params=request.url.query, content=body,
                headers={"X-Mobile-Proxy-Token": proxy_token,
                         **({"content-type": "application/json"} if body else {})},
                timeout=3600 if upstream.endswith("/turn") else 35)
        except httpx.RequestError:
            return JSONResponse({"error": "gateway unavailable"}, status_code=503, headers=HEADERS)
        headers = dict(HEADERS)
        if response.headers.get("content-type"):
            headers["Content-Type"] = response.headers["content-type"]
        return Response(response.content if request.method != "HEAD" else b"",
                        status_code=response.status_code, headers=headers)

    return app


app = create_app()
