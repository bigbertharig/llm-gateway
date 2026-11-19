"""Route and browser-origin checks for the Wi-Fi dashboard front door."""

import httpx
from fastapi.testclient import TestClient

from llm_gateway.lan_dashboard import MAX_BODY, create_app


def test_only_dashboard_routes_are_forwarded(monkeypatch):
    calls = []

    def upstream(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text="Rig dashboard")

    transport = httpx.MockTransport(upstream)
    monkeypatch.setenv("LAN_DASHBOARD_ALLOWED_HOSTS", "raspberrypi.local")
    with TestClient(create_app(transport)) as client:
        headers = {"host": "raspberrypi.local"}
        assert client.get("/gpurig", headers=headers).status_code == 200
        assert client.get("/v1/manager/status", headers=headers).status_code == 404
        assert client.get("/v1/manager/local-agent-capabilities", headers=headers).status_code == 200
        assert client.get("/health", headers=headers).status_code == 404
        assert client.get("/dashboard/%2e%2e/v1/manager/status", headers=headers).status_code == 404
        post_headers = {**headers, "origin": "https://raspberrypi.local",
                        "content-type": "application/json", "sec-fetch-site": "same-origin"}
        assert client.post("/dashboard/api/rig/kill", headers=post_headers, json={"gpu_ids": []}).status_code == 200
        assert client.post("/v1/manager/agent-routes/release", headers=post_headers, json={}).status_code == 200
        assert client.post("/dashboard/api/rig/kill", headers={**post_headers, "origin": "https://evil.test"}, json={}).status_code == 403
        assert client.post("/dashboard/api/rig/kill", headers={**post_headers, "sec-fetch-site": "cross-site"}, json={}).status_code == 403
        assert client.post("/dashboard/api/rig/rm_rf", headers=post_headers, json={}).status_code == 404
        assert client.post("/dashboard/api/rig/kill", headers=post_headers,
                           content=b"x" * (MAX_BODY + 1)).status_code == 413
        assert calls == [("GET", "/dashboard/"), ("GET", "/v1/manager/local-agent-capabilities"),
                         ("POST", "/dashboard/api/rig/kill"),
                         ("POST", "/v1/manager/agent-routes/release")]


def test_chat_stream_passes_through_lan_front_door(monkeypatch):
    monkeypatch.setenv("LAN_DASHBOARD_ALLOWED_HOSTS", "raspberrypi.local")
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"delta","text":"Hi"}\n\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200,
        headers={"content-type": "text/event-stream"},
        stream=Chunks()))
    headers = {"host": "raspberrypi.local", "origin": "https://raspberrypi.local",
               "content-type": "application/json", "accept": "text/event-stream",
               "sec-fetch-site": "same-origin"}
    with TestClient(create_app(transport)) as client:
        response = client.post("/dashboard/api/chat/conversations/" + "a" * 32 + "/turn",
                               headers=headers, json={"model": "local", "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert '"text":"Hi"' in response.text
