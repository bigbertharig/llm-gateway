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
        assert client.get("/health", headers=headers).status_code == 404
        assert client.get("/dashboard/%2e%2e/v1/manager/status", headers=headers).status_code == 404
        post_headers = {**headers, "origin": "https://raspberrypi.local",
                        "content-type": "application/json", "sec-fetch-site": "same-origin"}
        assert client.post("/dashboard/api/rig/kill", headers=post_headers, json={"gpu_ids": []}).status_code == 200
        assert client.post("/dashboard/api/rig/kill", headers={**post_headers, "origin": "https://evil.test"}, json={}).status_code == 403
        assert client.post("/dashboard/api/rig/kill", headers={**post_headers, "sec-fetch-site": "cross-site"}, json={}).status_code == 403
        assert client.post("/dashboard/api/rig/rm_rf", headers=post_headers, json={}).status_code == 404
        assert client.post("/dashboard/api/rig/kill", headers=post_headers,
                           content=b"x" * (MAX_BODY + 1)).status_code == 413
        assert calls == [("GET", "/dashboard/"), ("POST", "/dashboard/api/rig/kill")]
