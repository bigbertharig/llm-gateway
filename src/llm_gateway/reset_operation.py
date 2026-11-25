#!/usr/bin/env python3
"""Gateway-owned durable restart and saved-conversation recovery operation."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def turn_state(database: Path, conversation_id: str) -> str | None:
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT state FROM turns WHERE conversation_id=? "
            "ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,)).fetchone()
    return row[0] if row else None


def worker(args) -> int:
    request_path = Path(args.request_path).resolve()
    request_doc = json.loads(request_path.read_text(encoding="utf-8"))
    database = Path(args.data_root).resolve() / "conversations.sqlite3"
    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline and turn_state(database, args.conversation_id) == "running":
        time.sleep(1)
    request_doc.update(state="restarting", restarted_at=time.time())
    atomic_json(request_path, request_doc)
    result = subprocess.run(["sudo", "-n", "systemctl", "restart", args.service_unit],
                            text=True, capture_output=True)
    if result.returncode:
        request_doc.update(state="failed", error=(result.stderr or result.stdout).strip()[-500:])
        atomic_json(request_path, request_doc)
        return 1
    health_deadline = time.monotonic() + 60
    while time.monotonic() < health_deadline:
        try:
            with urlopen(args.gateway.rstrip("/") + "/health", timeout=2) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        time.sleep(1)
    else:
        request_doc.update(state="failed", error="gateway did not become healthy within 60 seconds")
        atomic_json(request_path, request_doc)
        return 1
    prompt = ("The requested gateway restart completed. Resume the work that was in progress before "
              "the restart. Inspect the saved thread and current workspace state first; do not repeat "
              "completed edits.\n\nRecovery note: " + request_doc["recovery_note"])
    payload = json.dumps({"model": "Codex", "message": prompt}).encode()
    try:
        with urlopen(Request(
                args.gateway.rstrip("/") + "/dashboard/api/chat/conversations/" +
                args.conversation_id + "/turn", data=payload,
                headers=resume_headers(), method="POST"), timeout=15) as response:
            accepted = json.load(response)
    except Exception as exc:
        request_doc.update(state="failed", error=f"resume request failed: {type(exc).__name__}: {exc}")
        atomic_json(request_path, request_doc)
        return 1
    request_doc.update(state="resumed", resumed_at=time.time(), turn_id=accepted.get("turn_id"))
    atomic_json(request_path, request_doc)
    return 0


def schedule(args, arguments: dict) -> dict:
    if arguments.get("confirmation") not in ("restart gateway", "restart pi-gateway"):
        raise ValueError('confirmation must be exactly "restart pi-gateway"')
    note = arguments.get("recovery_note")
    if not isinstance(note, str) or not 1 <= len(note) <= 1000:
        raise ValueError("recovery_note must describe what to continue in 1..1000 characters")
    service_unit = getattr(args, 'service_unit', None) or os.getenv('GATEWAY_SERVICE_UNIT', 'pi-gateway.service')
    gateway = getattr(args, 'gateway', None) or os.getenv('GATEWAY_LOCAL_URL', 'http://127.0.0.1:8080')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+[.]service', service_unit):
        raise ValueError('invalid service unit')
    from urllib.parse import urlsplit
    parsed = urlsplit(gateway)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('recovery endpoint must be loopback HTTP')
    root = Path(args.data_root).resolve()
    database = root / "conversations.sqlite3"
    if not database.is_file() or not args.conversation_id.isalnum():
        raise ValueError("saved conversation is unavailable")
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT provider,thread_id FROM conversations WHERE id=?",
                         (args.conversation_id,)).fetchone()
    if not row or row[0] != "codex" or not row[1]:
        raise ValueError("reset recovery requires an existing Codex thread")
    request_id = getattr(args, 'request_id', None) or uuid4().hex
    if not isinstance(request_id, str) or len(request_id) > 64 or not request_id.isalnum():
        raise ValueError('invalid request_id')
    request_dir = root / "reset-requests"
    request_dir.mkdir(mode=0o700, exist_ok=True)
    request_path = request_dir / f"{request_id}.json"
    if request_path.exists():
        previous = json.loads(request_path.read_text())
        if previous['conversation_id'] != args.conversation_id or previous['recovery_note'] != note:
            raise ValueError('request_id was already used for different arguments')
        return {'schema': 'gateway-reset/1', 'reset_id': request_id,
                'scheduled': previous['state'] in ('scheduled','restarting','resumed'), 'state': previous['state']}
    atomic_json(request_path, {"reset_id": request_id, "conversation_id": args.conversation_id,
        "thread_id": row[1], "state": "dispatching", "requested_at": time.time(),
        "recovery_note": note})
    unit = "llm-gateway-reset-" + request_id[:12]
    command = ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit", unit,
        "--uid", str(os.getuid()), sys.executable, str(Path(__file__).resolve()), "--worker",
        "--conversation-id", args.conversation_id, "--data-root", str(root),
        "--request-path", str(request_path), "--wait-seconds", str(args.wait_seconds),
        "--service-unit", service_unit, "--gateway", gateway]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        doc = json.loads(request_path.read_text(encoding="utf-8"))
        doc.update(state="failed", error=(result.stderr or result.stdout).strip()[-500:])
        atomic_json(request_path, doc)
        raise RuntimeError(doc["error"] or "could not schedule recovery unit")
    current = json.loads(request_path.read_text())
    if current['state'] == 'dispatching':
        current['state'] = 'scheduled'
        atomic_json(request_path, current)
    return {"schema": "gateway-reset/1", "state": current['state'], "scheduled": True, "reset_id": request_id, "unit": unit,
            "message": "Restart is scheduled. Finish this response promptly; recovery will continue the saved thread."}


def resume_headers():
    headers = {'Content-Type': 'application/json'}
    key_file = Path(os.getenv('GATEWAY_API_KEYS_FILE', '/etc/llm-gateway/api_keys'))
    if key_file.is_file():
        keys = [s.strip() for s in key_file.read_text().splitlines()
                if s.strip() and not s.strip().startswith('#')]
        if keys:
            headers['Authorization'] = 'Bearer ' + keys[0]
    return headers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-unit', required=True)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--worker', action='store_true', required=True)
    parser.add_argument('--conversation-id', required=True)
    parser.add_argument('--data-root', required=True)
    parser.add_argument('--request-path', required=True)
    parser.add_argument('--wait-seconds', type=int, default=180)
    args = parser.parse_args()
    return worker(args)

if __name__ == '__main__':
    raise SystemExit(main())
