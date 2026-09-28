from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .catalog import CatalogError, load_openrouter_models
from .settings import Settings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _upstream_error(response: httpx.Response) -> Response:
    content_type = response.headers.get("content-type", "application/json")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=content_type.split(";", 1)[0],
    )


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configured.validate()
        load_openrouter_models(configured.model_catalog_path)
        timeout = httpx.Timeout(configured.request_timeout_seconds, connect=30.0)
        app.state.http = httpx.AsyncClient(timeout=timeout, transport=transport)
        yield
        await app.state.http.aclose()

    app = FastAPI(title="Big Bertha LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = configured

    async def require_gateway_auth(request: Request) -> None:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {configured.gateway_api_key}"
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="invalid gateway credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    auth = Depends(require_gateway_auth)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    async def local_models(request: Request) -> tuple[list[dict], bool]:
        headers = _bearer(configured.orchestrator_api_key)
        try:
            response = await request.app.state.http.get(
                f"{configured.orchestrator_url}/v1/local/models", headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            models = payload.get("models", [])
            if not isinstance(models, list):
                raise ValueError("orchestrator models response is not a list")
            return models, True
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return [], False

    @app.get("/v1/models", dependencies=[auth])
    async def models(request: Request, response: Response) -> dict:
        try:
            cloud_ids = load_openrouter_models(configured.model_catalog_path)
        except CatalogError as exc:
            raise HTTPException(500, str(exc)) from exc

        local, available = await local_models(request)
        response.headers["X-Local-Models-Status"] = "available" if available else "unavailable"
        data = [
            {"id": f"openrouter/{model_id}", "object": "model", "owned_by": "openrouter"}
            for model_id in cloud_ids
        ]
        for item in local:
            model_id = str(item.get("id") or item.get("model_id") or "").strip()
            if model_id:
                data.append(
                    {"id": f"local/{model_id}", "object": "model", "owned_by": "local"}
                )
        return {"object": "list", "data": data}

    async def acquire_local(request: Request, model_id: str) -> dict:
        headers = {
            "Content-Type": "application/json",
            **_bearer(configured.orchestrator_api_key),
        }
        encoded = quote(model_id, safe="")
        try:
            response = await request.app.state.http.post(
                f"{configured.orchestrator_url}/v1/local/models/{encoded}/acquire",
                headers=headers,
                json={"ttl_seconds": configured.local_session_ttl_seconds},
            )
        except httpx.RequestError as exc:
            raise HTTPException(502, "local orchestrator is unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.text)
        try:
            payload = response.json()
            endpoint = str(payload["endpoint"]).rstrip("/")
            runtime_model = str(payload["runtime_model"])
            session_id = str(payload["session_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(502, "invalid orchestrator acquire response") from exc
        return {
            **payload,
            "endpoint": endpoint,
            "runtime_model": runtime_model,
            "session_id": session_id,
        }

    async def release_local(request: Request, session_id: str) -> None:
        encoded = quote(session_id, safe="")
        with suppress(httpx.HTTPError):
            await request.app.state.http.delete(
                f"{configured.orchestrator_url}/v1/local/sessions/{encoded}",
                headers=_bearer(configured.orchestrator_api_key),
            )

    async def renew_local(request: Request, session_id: str, stop: asyncio.Event) -> None:
        encoded = quote(session_id, safe="")
        headers = {
            "Content-Type": "application/json",
            **_bearer(configured.orchestrator_api_key),
        }
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=configured.local_session_renew_seconds
                )
                return
            except TimeoutError:
                pass
            try:
                response = await request.app.state.http.post(
                    f"{configured.orchestrator_url}/v1/local/sessions/{encoded}/renew",
                    headers=headers,
                    json={"ttl_seconds": configured.local_session_ttl_seconds},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                return

    async def resolve_target(request: Request, model: str) -> tuple[str, str, dict, str | None]:
        if model.startswith("openrouter/"):
            model_id = model.removeprefix("openrouter/")
            allowed = load_openrouter_models(configured.model_catalog_path)
            if model_id not in allowed:
                raise HTTPException(404, f"OpenRouter model is not configured: {model_id}")
            if not configured.openrouter_api_key:
                raise HTTPException(503, "OpenRouter is not configured")
            return (
                configured.openrouter_base_url,
                model_id,
                _bearer(configured.openrouter_api_key),
                None,
            )

        if model.startswith("local/"):
            model_id = model.removeprefix("local/")
            if not model_id:
                raise HTTPException(400, "local model id is required")
            acquired = await acquire_local(request, model_id)
            return (
                acquired["endpoint"],
                acquired["runtime_model"],
                {},
                acquired["session_id"],
            )

        raise HTTPException(400, "model must start with openrouter/ or local/")

    @app.post("/v1/chat/completions", dependencies=[auth])
    async def chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(400, "request body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "request body must be a JSON object")
        model = body.get("model")
        if not isinstance(model, str) or not model:
            raise HTTPException(400, "model is required")

        base_url, upstream_model, auth_headers, session_id = await resolve_target(
            request, model
        )
        upstream_body = dict(body)
        upstream_body["model"] = upstream_model
        headers = {"Content-Type": "application/json", **auth_headers}
        url = f"{base_url}/chat/completions"

        stop_renewal = asyncio.Event()
        renewal_task = (
            asyncio.create_task(renew_local(request, session_id, stop_renewal))
            if session_id
            else None
        )

        if not bool(upstream_body.get("stream")):
            try:
                upstream = await request.app.state.http.post(
                    url, headers=headers, json=upstream_body
                )
                return _upstream_error(upstream)
            except httpx.RequestError as exc:
                raise HTTPException(502, "model endpoint is unavailable") from exc
            finally:
                stop_renewal.set()
                if renewal_task:
                    await renewal_task
                if session_id:
                    await release_local(request, session_id)

        upstream_request = request.app.state.http.build_request(
            "POST", url, headers=headers, json=upstream_body
        )
        try:
            upstream = await request.app.state.http.send(upstream_request, stream=True)
        except httpx.RequestError as exc:
            stop_renewal.set()
            if renewal_task:
                await renewal_task
            if session_id:
                await release_local(request, session_id)
            raise HTTPException(502, "model endpoint is unavailable") from exc

        if upstream.status_code >= 400:
            await upstream.aread()
            await upstream.aclose()
            stop_renewal.set()
            if renewal_task:
                await renewal_task
            if session_id:
                await release_local(request, session_id)
            return _upstream_error(upstream)

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
                stop_renewal.set()
                if renewal_task:
                    await renewal_task
                if session_id:
                    await release_local(request, session_id)

        media_type = upstream.headers.get("content-type", "text/event-stream").split(";", 1)[0]
        return StreamingResponse(relay(), status_code=upstream.status_code, media_type=media_type)

    return app


app = create_app()

