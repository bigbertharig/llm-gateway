"""The remote front door must not inherit LAN admin routes or tool grants."""

import httpx
from fastapi.testclient import TestClient

from llm_gateway.dashboard import dashboard_mcp_servers
from llm_gateway.mobile_dashboard import STATIC, create_app


def test_mobile_identity_and_permissions_are_checked_before_forwarding(monkeypatch):
    calls = []
    def upstream(request):
        calls.append((request.method, request.url.path,
                      request.headers.get("x-mobile-proxy-token")))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setenv("MOBILE_TAILSCALE_USER", "operator@example.invalid")
    monkeypatch.setenv("MOBILE_TAILSCALE_HOST", "pi.example.ts.net")
    monkeypatch.setenv("MOBILE_PROXY_TOKEN", "a" * 48)
    monkeypatch.delenv("MOBILE_PERMISSIONS", raising=False)
    identity = {"host": "pi.example.ts.net", "tailscale-user-login": "operator@example.invalid"}
    post = {**identity, "origin": "https://pi.example.ts.net", "sec-fetch-site": "same-origin"}
    cid = "b" * 32
    with TestClient(create_app(httpx.MockTransport(upstream))) as client:
        assert client.get("/mobile/api/conversations", headers={"host": "pi.example.ts.net"}).status_code == 403
        assert client.get("/mobile/api/conversations", headers={**identity,
            "tailscale-user-login": "unapproved@example.invalid"}).status_code == 403
        assert client.get("/mobile/api/conversations", headers=identity).status_code == 200
        assert client.get("/dashboard/api/chat/conversations", headers=identity).status_code == 404
        assert client.get("/mobile/api/rig/kill", headers=identity).status_code == 404
        assert client.get("/mobile/api/conversations/" + cid + "/agent/events", headers=identity).status_code == 404
        assert client.post("/mobile/api/rig/kill", headers=post, json={"gpu_ids": [0]}).status_code == 403
        assert client.post("/mobile/api/conversations/" + cid + "/turn",
                           headers={**post, "origin": "https://evil.invalid"}, json={}).status_code == 403
        assert client.post("/mobile/api/conversations/" + cid + "/turn",
                           headers=post, json={"model": "Codex", "message": "Continue"}).status_code == 200
        assert client.get("/mobile/api/conversations/" + cid + "/../rig", headers=identity).status_code == 404
    assert calls == [("GET", "/dashboard/api/chat/conversations", "a" * 48),
                     ("POST", "/dashboard/api/chat/conversations/" + cid + "/turn", "a" * 48)]


def test_mobile_tool_catalog_does_not_inherit_local_controls(monkeypatch):
    monkeypatch.delenv("MOBILE_TOOL_SERVERS", raising=False)
    assert "local_control" not in dashboard_mcp_servers.surface_servers("chat", "mobile")
    assert "private_control" not in dashboard_mcp_servers.surface_servers("chat", "mobile")
    assert "local_agents" not in dashboard_mcp_servers.surface_servers("chat", "mobile")
    assert "local_agents" in dashboard_mcp_servers.surface_servers("chat", "local")


def test_mobile_streamed_turn_reaches_gateway_as_stream(monkeypatch):
    seen = []
    def upstream(request):
        seen.append((request.url.path, request.headers.get("x-mobile-proxy-token"),
                     request.read()))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: {"type":"done"}\n\n')
    monkeypatch.setenv("MOBILE_TAILSCALE_USER", "operator@example.invalid")
    monkeypatch.setenv("MOBILE_TAILSCALE_HOST", "pi.example.ts.net")
    monkeypatch.setenv("MOBILE_PROXY_TOKEN", "a" * 48)
    cid = "b" * 32
    with TestClient(create_app(httpx.MockTransport(upstream))) as client:
        response = client.post(f"/mobile/api/conversations/{cid}/turn", headers={
            "host": "pi.example.ts.net", "tailscale-user-login": "operator@example.invalid",
            "origin": "https://pi.example.ts.net", "sec-fetch-site": "same-origin"},
            json={"model": "Local", "message": "continue", "stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == 'data: {"type":"done"}\n\n'
    assert seen[0][0] == f"/dashboard/api/chat/conversations/{cid}/turn"
    assert seen[0][1] == "a" * 48
    assert b'"stream":true' in seen[0][2]


def test_mobile_chat_has_distinct_tool_style_attachments_and_live_turn_check():
    page = STATIC.read_text(encoding="utf-8")
    assert ".message.assistant{color:#fff}" in page
    assert ".tool-message" in page and "color:#9eabb2" in page and "'tool-message'" in page
    assert 'id="attachImage"' in page and 'id="attachFile"' in page
    assert 'accept="image/png,image/jpeg,image/webp"' in page
    assert "state.active===true&&state.turn?.state==='running'" in page
    assert "if(error.status!==409)throw error" in page
    assert 'id="composerStatus"' in page
    assert "catch(error){composerError(error.message)}" in page
