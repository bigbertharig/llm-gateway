"""Gateway tests with a fake `rig` command and a fake llama-server. Run: python3 -m unittest -v"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from llm_gateway.server import Gateway, make_server  # noqa: E402
from llm_gateway.settings import Settings  # noqa: E402

# Fake rig: state in $FAKE_RIG_STATE; behavior of `start` in $FAKE_RIG_START (ok|busy|nocap|fail).
FAKE_RIG = textwrap.dedent("""\
    import json, os, sys, time
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
        time.sleep(0.5)
        st["profiles"].append({"id": args[2], "port": int(os.environ["FAKE_UPSTREAM_PORT"]), "state": "ready"})
        json.dump(st, open(state_path, "w")); sys.exit(0)
    sys.exit(64)
""")


class FakeLlama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for tok in ("o", "k"):
                self.wfile.write(f'data: {{"choices":[{{"delta":{{"content":"{tok}"}}}}]}}\n\n'.encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
            return
        out = json.dumps({"choices": [{"message": {"content": "ok"}}], "echo_model": body.get("model")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        (self.tmp / "rig.py").write_text(FAKE_RIG)
        self.state = self.tmp / "state.json"
        self.state.write_text(json.dumps({"mode": "idle", "status": "idle", "profiles": []}))
        self.lib = self.tmp / "lib.json"
        self.lib.write_text(json.dumps({"remote_profiles": [
            {"id": "coder-7b", "model": "qwen2.5-coder:7b", "ctx_size": 16384, "description": "small"},
            {"id": "chat-27b", "model": "qwen3.6:27b", "ctx_size": 65536, "description": "big"},
        ]}))
        os.environ.update(FAKE_RIG_STATE=str(self.state), FAKE_RIG_START="ok",
                          FAKE_UPSTREAM_PORT=str(self.upstream.server_port))
        settings = Settings(
            rig_command=(sys.executable, str(self.tmp / "rig.py")), profiles_path=self.lib,
            upstream_host="127.0.0.1", model_prefix="gpu-rig", bind="127.0.0.1", port=0,
            start_timeout_seconds=30, request_timeout_seconds=30,
        )
        self.server = make_server(Gateway(settings))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        for srv in (self.server, self.upstream):
            srv.shutdown()
            srv.server_close()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.getheader("Content-Type"), data

    def chat(self, model="gpu-rig/coder-7b", **extra):
        return self.request("POST", "/v1/chat/completions",
                            {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra})

    def starts(self):
        p = Path(str(self.state) + ".starts")
        return p.read_text().split() if p.exists() else []

    def test_models_lists_profiles_with_context(self):
        status, _, data = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        models = {m["id"]: m for m in json.loads(data)["data"]}
        self.assertEqual(set(models), {"gpu-rig/coder-7b", "gpu-rig/chat-27b"})
        self.assertEqual(models["gpu-rig/chat-27b"]["context_length"], 65536)

    def test_models_follow_library_edits(self):
        time.sleep(0.01)
        lib = json.loads(self.lib.read_text())
        lib["remote_profiles"].append({"id": "new-one", "model": "m", "ctx_size": 4096})
        self.lib.write_text(json.dumps(lib))
        os.utime(self.lib, (time.time() + 5, time.time() + 5))
        _, _, data = self.request("GET", "/v1/models")
        self.assertIn("gpu-rig/new-one", [m["id"] for m in json.loads(data)["data"]])

    def test_unknown_model_404_lists_published(self):
        status, _, data = self.chat(model="gpu-rig/nope")
        self.assertEqual(status, 404)
        self.assertIn("gpu-rig/coder-7b", json.loads(data)["error"]["message"])

    def test_cold_start_loads_then_forwards_body_unchanged(self):
        status, _, data = self.chat()
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
        self.assertEqual(payload["echo_model"], "gpu-rig/coder-7b")
        self.assertEqual(self.starts(), ["coder-7b"])

    def test_warm_request_does_not_start(self):
        self.chat()
        self.chat()
        self.assertEqual(self.starts(), ["coder-7b"])

    def test_concurrent_cold_requests_share_one_start(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.chat()[0])) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 4)
        self.assertEqual(self.starts(), ["coder-7b"])

    def test_streaming_passthrough(self):
        status, ctype, data = self.chat(stream=True)
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/event-stream")
        self.assertIn(b'"content":"o"', data)
        self.assertTrue(data.rstrip().endswith(b"data: [DONE]"))

    def test_busy_rig_returns_503(self):
        os.environ["FAKE_RIG_START"] = "busy"
        status, _, data = self.chat()
        self.assertEqual(status, 503)
        self.assertIn("rig busy: busy: bench", json.loads(data)["error"]["message"])

    def test_no_capacity_returns_503(self):
        os.environ["FAKE_RIG_START"] = "nocap"
        self.assertEqual(self.chat()[0], 503)

    def test_failed_load_returns_502(self):
        os.environ["FAKE_RIG_START"] = "fail"
        status, _, data = self.chat()
        self.assertEqual(status, 502)
        self.assertIn("container exited", json.loads(data)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
