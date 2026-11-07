"""TLS client-certificate front door for the local dashboard only.

The main gateway remains on Pi loopback. Nginx enforces client TLS
certificates and advertises the trusted CA; this app limits HTTP routes.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

UPSTREAM = "http://127.0.0.1:8080"
GET_PATHS = frozenset({
    "/gpurig", "/gpurig/", "/dashboard", "/dashboard/", "/dashboard/api/rig", "/dashboard/api/models",
    "/dashboard/api/benchmarks", "/dashboard/api/orchestration",
    "/dashboard/api/orchestration/options",
    "/dashboard/api/chat/options", "/dashboard/api/chat/conversations",
})
RIG_ACTIONS = frozenset({"kill", "unload", "unclaim", "cancel", "load"})
RUN_ACTIONS = frozenset({"recover", "dismiss", "stop"})
ORCHESTRATION_ACTIONS = frozenset({
    "start_plan", "resume_plan", "kill_plan", "kill_all_active",
    "batch_outputs", "cleanup_stale", "requeue_worker",
})
MAX_BODY = 1_048_576
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; connect-src 'self'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'"
    ),
}


def _allowed(method: str, path: str) -> bool:
    if method in ("GET", "HEAD"):
        return path in GET_PATHS or (path.startswith("/dashboard/api/chat/conversations/")
                                     and len(path.rsplit("/", 1)[-1]) == 32)
    if method == "DELETE":
        return path.startswith("/dashboard/api/chat/conversations/") and len(path.rsplit("/", 1)[-1]) == 32
    if method != "POST":
        return False
    if path == "/dashboard/api/chat/conversations":
        return True
    if path.startswith("/dashboard/api/chat/conversations/") and path.endswith(("/turn", "/model", "/prepare")):
        return len(path.split("/")[-2]) == 32
    if path.startswith("/dashboard/api/rig/run/"):
        return path.rsplit("/", 1)[-1] in RUN_ACTIONS
    if path.startswith("/dashboard/api/rig/"):
        return path.rsplit("/", 1)[-1] in RIG_ACTIONS
    if path.startswith("/dashboard/api/orchestration/"):
        return path.rsplit("/", 1)[-1] in ORCHESTRATION_ACTIONS
    return False


def create_app(transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(base_url=UPSTREAM, timeout=35, transport=transport,
                                     trust_env=False, follow_redirects=False) as client:
            app.state.client = client
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "DELETE"])
    async def proxy(path: str, request: Request):
        route = request.url.path
        raw = request.scope.get("raw_path", b"")
        if b"%" in raw or not _allowed(request.method, route):
            return JSONResponse({"error": "not found"}, status_code=404, headers=SECURITY_HEADERS)
        allowed_hosts = os.getenv("LAN_DASHBOARD_ALLOWED_HOSTS", "")
        host = request.headers.get("host", "").lower()
        if host not in {h.strip().lower() for h in allowed_hosts.split(",")}:
            return JSONResponse({"error": "invalid host"}, status_code=400, headers=SECURITY_HEADERS)
        if request.method in ("POST", "DELETE"):
            if request.headers.get("origin") != f"https://{host}":
                return JSONResponse({"error": "invalid origin"}, status_code=403, headers=SECURITY_HEADERS)
            if request.headers.get("sec-fetch-site", "same-origin") != "same-origin":
                return JSONResponse({"error": "cross-site request"}, status_code=403, headers=SECURITY_HEADERS)
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return JSONResponse({"error": "JSON required"}, status_code=415, headers=SECURITY_HEADERS)
            try:
                declared_length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"error": "invalid length"}, status_code=400, headers=SECURITY_HEADERS)
            if declared_length < 0 or declared_length > MAX_BODY:
                return JSONResponse({"error": "request too large"}, status_code=413, headers=SECURITY_HEADERS)
            chunks = bytearray()
            async for chunk in request.stream():
                chunks.extend(chunk)
                if len(chunks) > MAX_BODY:
                    return JSONResponse({"error": "request too large"}, status_code=413,
                                        headers=SECURITY_HEADERS)
            body = bytes(chunks)
        else:
            body = b""
        try:
            upstream_route = "/dashboard/" if route in ("/gpurig", "/gpurig/") else route
            result = await request.app.state.client.request(
                request.method, upstream_route, params=request.url.query,
                content=body, headers={"content-type": "application/json"} if body else {},
                timeout=900 if route.endswith(("/turn", "/prepare")) else 35,
            )
        except httpx.RequestError:
            return JSONResponse({"error": "dashboard unavailable"}, status_code=503, headers=SECURITY_HEADERS)
        headers = dict(SECURITY_HEADERS)
        if "location" in result.headers:
            headers["Location"] = result.headers["location"]
        if "content-type" in result.headers:
            headers["Content-Type"] = result.headers["content-type"]
        return Response(result.content if request.method != "HEAD" else b"", status_code=result.status_code,
                        headers=headers)

    return app


app = create_app()
