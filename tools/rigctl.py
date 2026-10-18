#!/usr/bin/env python3
"""rigctl: command-line client of the rig manager (docs/MANAGER.md). Standard library only.

Runs on the Pi, or anywhere over ssh (the rig: `ssh pi python3 ~/llm-gateway/tools/rigctl.py ...`).

    rigctl status [--json]
    rigctl acquire --controller benchmarks --owner lanes --run-id run1 --gpus 0
    rigctl acquire --controller benchmarks --owner lanes --run-id run1 --need 5800:8192 --need 5800:8192
    rigctl renew LEASE --owner lanes [--ttl 120]
    rigctl release LEASE --owner lanes | --force
    rigctl claim NAME --gpus 0 [--note "roo testing"]      rigctl unclaim NAME
    rigctl load PROFILE [--claim NAME]                     rigctl unload PROFILE

Exit codes follow `rig`: 0 ok, 1 failed, 2 refused (held, not admitted, wrong owner), 4 no capacity.
Env: RIGCTL_URL (default http://127.0.0.1:8080), RIGCTL_KEY (bearer key, if the gateway has keys).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.getenv("RIGCTL_URL", "http://127.0.0.1:8080").rstrip("/")
EXIT = {200: 0, 201: 0, 400: 1, 403: 2, 404: 1, 409: 2, 502: 1, 503: 4, 504: 1}


def call(method: str, path: str, body: dict | None = None, timeout: float = 1000) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if os.getenv("RIGCTL_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['RIGCTL_KEY']}"
    req = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": {"message": exc.reason}}
    except urllib.error.URLError as exc:
        return 0, {"error": {"message": f"manager unreachable at {URL}: {exc.reason}"}}


def need(text: str) -> dict:
    lo, _, hi = text.partition(":")
    return {"min_vram_mb": int(lo), **({"max_vram_mb": int(hi)} if hi else {})}


def print_status(s: dict) -> None:
    print(f"rig mode {s.get('mode')}  observed {s.get('observed_at')}  {s.get('summary')}")
    for g in s["gpus"]:
        lease, obs = g["lease"], g["observed"]
        held = f"{lease['controller']}/{lease['owner']} {lease['model'] or ''} :{lease['port']}" if lease else "-"
        claim = f" claim={g['claim']}" if g["claim"] else ""
        print(f"  GPU{g['gpu']} {g['verdict']:<14} {held:<44}{claim}  {obs['used_mb']} MB "
              f"{obs['util_pct']}% {obs['power_w']} W  {','.join(obs['containers']) or '-'}")
    for j in s.get("jobs", []):
        print(f"  job {j['job']} {j['active_state']}" + (f"\n{j['log_tail']}" if j.get("log_tail") else ""))
    if s.get("last_error"):
        print(f"  rig last error: {s['last_error']}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="rigctl", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    sub.add_parser("leases")
    s = sub.add_parser("acquire")
    s.add_argument("--controller", required=True)
    s.add_argument("--owner", required=True)
    s.add_argument("--run-id", required=True)
    s.add_argument("--gpus", help="explicit GPU ids, e.g. 0 or 1,2")
    s.add_argument("--need", action="append", type=need, help="per card MIN[:MAX] VRAM in MB (repeat per card)")
    s.add_argument("--claim")
    s.add_argument("--ttl", type=int, default=120)
    s.add_argument("--model", default="")
    s.add_argument("--purpose", default="")
    s = sub.add_parser("renew"); s.add_argument("lease"); s.add_argument("--owner", required=True)
    s.add_argument("--ttl", type=int, default=120)
    s = sub.add_parser("release"); s.add_argument("lease"); s.add_argument("--owner", default="")
    s.add_argument("--force", action="store_true")
    s = sub.add_parser("claim"); s.add_argument("name"); s.add_argument("--gpus", required=True)
    s.add_argument("--note", default=""); s.add_argument("--by", default=os.getenv("USER", ""))
    s = sub.add_parser("unclaim"); s.add_argument("name")
    s = sub.add_parser("load"); s.add_argument("profile"); s.add_argument("--claim")
    s = sub.add_parser("unload"); s.add_argument("profile")
    a = ap.parse_args()

    if a.cmd == "status":
        code, out = call("GET", "/v1/manager/status")
        if code == 200 and not a.json:
            print_status(out)
            return 0
    elif a.cmd == "leases":
        code, out = call("GET", "/v1/manager/leases")
    elif a.cmd == "acquire":
        body = {"controller": a.controller, "owner": a.owner, "run_id": a.run_id, "ttl_s": a.ttl,
                "model": a.model, "purpose": a.purpose, "claim": a.claim}
        if a.gpus:
            body["gpu_ids"] = [int(g) for g in a.gpus.split(",")]
        if a.need:
            body["gpus_needed"] = a.need
        code, out = call("POST", "/v1/manager/leases", body)
    elif a.cmd == "renew":
        code, out = call("POST", f"/v1/manager/leases/{a.lease}/renew", {"owner": a.owner, "ttl_s": a.ttl})
    elif a.cmd == "release":
        code, out = call("POST", f"/v1/manager/leases/{a.lease}/release", {"owner": a.owner, "force": a.force})
    elif a.cmd == "claim":
        code, out = call("POST", "/v1/manager/claims", {"name": a.name, "note": a.note, "by": a.by,
                                                         "gpu_ids": [int(g) for g in a.gpus.split(",")]})
    elif a.cmd == "unclaim":
        code, out = call("POST", f"/v1/manager/claims/{a.name}/release")
    elif a.cmd == "load":
        code, out = call("POST", f"/v1/manager/profiles/{a.profile}/load", {"claim": a.claim})
    else:
        code, out = call("POST", f"/v1/manager/profiles/{a.profile}/unload")
    print(json.dumps(out, indent=2))
    return EXIT.get(code, 1)


if __name__ == "__main__":
    sys.exit(main())
