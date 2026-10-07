from __future__ import annotations

import http.client
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .profiles import ProfileError, Profiles
from .rig import Rig, RigUnavailable
from .settings import Settings

log = logging.getLogger("llm_gateway")

# OpenAI-compatible inference routes forwarded to the profile's llama-server.
FORWARD_PATHS = ("/v1/chat/completions", "/v1/completions")


class GatewayError(Exception):
    def __init__(self, status: int, message: str, kind: str = "gateway_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


class Gateway:
    def __init__(self, settings: Settings, rig: Rig | None = None, profiles: Profiles | None = None):
        self.settings = settings
        self.rig = rig or Rig(settings.rig_command, settings.start_timeout_seconds)
        self.profiles = profiles or Profiles(settings.profiles_path)

    def model_list(self) -> dict:
        prefix = self.settings.model_prefix
        data = [
            {
                "id": f"{prefix}/{pid}",
                "object": "model",
                "created": 0,
                "owned_by": prefix,
                "description": p.get("description", ""),
                "context_length": p.get("ctx_size"),
            }
            for pid, p in self.profiles.all().items()
        ]
        return {"object": "list", "data": data}

    def resolve_profile(self, model: object) -> str:
        prefix = self.settings.model_prefix + "/"
        published = self.profiles.all()
        names = sorted(prefix + pid for pid in published)
        if not isinstance(model, str) or not model.startswith(prefix):
            raise GatewayError(404, f"model must be one of {names}", "model_not_found")
        pid = model[len(prefix):]
        if pid not in published:
            raise GatewayError(404, f"unknown model {model!r}; published: {names}", "model_not_found")
        return pid


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway: Gateway  # set by make_server

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ helpers
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str, kind: str) -> None:
        self._json(status, {"error": {"message": message, "type": kind, "code": status}})

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ------------------------------------------------------------ routes
    def do_GET(self):
        try:
            if self.path == "/health":
                return self._json(200, {"status": "ok"})
            if self.path == "/v1/models":
                return self._json(200, self.gateway.model_list())
            if self.path == "/v1/rig/status":
                return self._json(200, self.gateway.rig.status())
            self._error(404, f"no route {self.path}", "not_found")
        except ProfileError as exc:
            self._error(500, str(exc), "profiles_error")
        except RigUnavailable as exc:
            self._error(exc.status, str(exc), "rig_unavailable")

    def do_POST(self):
        if self.path not in FORWARD_PATHS:
            return self._error(404, f"no route {self.path}", "not_found")
        raw = self._read_body()
        try:
            body = json.loads(raw or b"null")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            pid = self.gateway.resolve_profile(body.get("model"))
            port = self.gateway.rig.ensure_ready(pid)
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, f"invalid request body: {exc}", "invalid_request")
        except GatewayError as exc:
            return self._error(exc.status, str(exc), exc.kind)
        except ProfileError as exc:
            return self._error(500, str(exc), "profiles_error")
        except RigUnavailable as exc:
            return self._error(exc.status, str(exc), "rig_unavailable")
        self._forward(port, raw, stream=bool(body.get("stream")))

    def _forward(self, port: int, raw: bytes, stream: bool) -> None:
        """Pass the request body through unchanged (llama-server ignores `model`)."""
        s = self.gateway.settings
        conn = http.client.HTTPConnection(s.upstream_host, port, timeout=s.request_timeout_seconds)
        try:
            conn.request("POST", self.path, body=raw, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except OSError as exc:
            conn.close()
            return self._error(502, f"model endpoint :{port} unavailable: {exc}", "upstream_error")
        try:
            ctype = resp.getheader("Content-Type", "application/json")
            if not stream or resp.status >= 400:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected during response")
        finally:
            conn.close()


def make_server(gateway: Gateway) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"gateway": gateway})
    server = ThreadingHTTPServer((gateway.settings.bind, gateway.settings.port), handler)
    server.daemon_threads = True
    return server
