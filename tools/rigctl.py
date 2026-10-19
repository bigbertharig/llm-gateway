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
    rigctl fit MODEL --cards 2 | --gpus 1,3 [--ctx N] [--use benchmark|chat|worker] [--profile P]
    rigctl load-model MODEL --cards 2 | --gpus 1,3 [--ctx N] [--use ...] [--args "--reasoning on"] [--json]
                                                           any registry model on any cards; the harness
                                                           checks the fit first (exit 4: won't fit), the
                                                           rig loads it; unload with `rigctl unload <profile>`
    rigctl controller                                      who is in control (this one, the active one)
    rigctl takeover                                        explicit handoff: this controller takes control
    rigctl plan [--json]                                   the rig's queue (now and next)
    rigctl cancel ITEM                                     cancel a queued plan item

Exit codes follow `rig`: 0 ok, 1 failed, 2 refused (held, not admitted, wrong owner), 4 no capacity.
Env: RIGCTL_URL (default http://127.0.0.1:8080), RIGCTL_KEY (bearer key, if the gateway has keys).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.error
import urllib.request

URL = os.getenv("RIGCTL_URL", "http://127.0.0.1:8080").rstrip("/")
EXIT = {200: 0, 201: 0, 400: 1, 403: 2, 404: 1, 409: 2, 422: 4, 502: 1, 503: 4, 504: 1}


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


def print_plan(plan: dict, limit: int = 8) -> None:
    for i in (plan.get("items") or [])[-limit:]:
        result = str(i.get("result") or "").splitlines()[-1:] if i["state"] in ("failed",) else []
        print(f"  plan {i['id']:<20} {i['state']:<9} {i['kind']:<6} {json.dumps(i['args'])}"
              + (f"  ({result[0][:120]})" if result else ""))


def print_fit(f: dict) -> None:
    print(f.get("reason", ""))
    if f.get("tensor_split"):
        print(f"  tensor split {f['tensor_split']} (layers per card, output on the last)")
    for c in f.get("cards") or []:
        print(f"  GPU{c['gpu']} {c['layers']:>3} layers  weights {c['weights_mb']} + KV {c['kv_mb']} + compute "
              f"{c['compute_mb']} MiB = {c['projected_mb']} of {c['free_mb']} free (headroom {c['headroom_mb']})")
    for n in f.get("notes") or []:
        print(f"  note: {n}")


def print_status(s: dict) -> None:
    print(f"rig mode {s.get('mode')}  observed {s.get('observed_at')}  {s.get('summary')}")
    c = s.get("controller") or {}
    active = c.get("active") or {}
    print(f"controller: this={c.get('this')} {'IN CONTROL' if c.get('in_control') else 'read-only'}; "
          f"active={active.get('id', 'none')}" + (" (stale)" if active.get("stale") else "")
          + (f", heartbeat {active.get('heartbeat_at')}" if active else ""))
    g = s.get("guard")
    if g:
        print(f"guard {g['level']} (loads {'admitted' if g['loads_admitted'] else 'held'}): {g['why']}  "
              f"[GPU {g.get('gpu_power_total_w')} W, max {g.get('gpu_temp_max_c')} C; "
              f"CPU {g.get('cpu_package_c')} C; {g.get('mem_available_mb')} MB free]")
    for g in s["gpus"]:
        lease, obs = g["lease"], g["observed"]
        held = f"{lease['controller']}/{lease['owner']} {lease['model'] or ''} :{lease['port']}" if lease else "-"
        claim = f" claim={g['claim']}" if g["claim"] else ""
        print(f"  GPU{g['gpu']} {g['verdict']:<14} {held:<44}{claim}  {obs['used_mb']} MB "
              f"{obs['util_pct']}% {obs['power_w']} W  {','.join(obs['containers']) or '-'}")
    print_plan(s.get("plan") or {}, limit=5)
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
    for name in ("fit", "load-model"):
        s = sub.add_parser(name); s.add_argument("model")
        s.add_argument("--cards", type=int, help="how many cards (smallest free cards first)")
        s.add_argument("--gpus", help="explicit GPU ids, e.g. 1,3")
        s.add_argument("--ctx", help="exact context that must fit (default auto: the largest that fits)")
        s.add_argument("--ctx-min", type=int, help="smallest acceptable context for auto (default 4096)")
        s.add_argument("--use", default="benchmark", choices=("benchmark", "chat", "worker"))
        s.add_argument("--profile", help="registry profile (default: by use)")
        s.add_argument("--claim"); s.add_argument("--margin-mb", type=int)
        s.add_argument("--json", action="store_true")
        if name == "load-model":
            s.add_argument("--args", default="", help='extra llama-server args after the registry ones, e.g. "--reasoning on"')
    sub.add_parser("controller")
    sub.add_parser("takeover")
    s = sub.add_parser("plan"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("cancel"); s.add_argument("item")
    a = ap.parse_args()

    if a.cmd == "controller":
        code, out = call("GET", "/v1/manager/controller")
    elif a.cmd == "takeover":
        code, out = call("POST", "/v1/manager/controller/claim", {"force": True})
    elif a.cmd == "plan":
        code, out = call("GET", "/v1/manager/plan")
        if code == 200 and not a.json:
            print_plan(out, limit=20)
            return 0
    elif a.cmd == "cancel":
        code, out = call("POST", f"/v1/manager/plan/{a.item}/cancel")
    if a.cmd in ("controller", "takeover", "plan", "cancel"):
        print(json.dumps(out, indent=2))
        return EXIT.get(code, 1)

    if a.cmd in ("fit", "load-model"):
        body = {"model": a.model, "cards": a.cards, "ctx": a.ctx, "ctx_min": a.ctx_min, "use": a.use,
                "profile": a.profile, "claim": a.claim, "margin_mb": a.margin_mb,
                "gpu_ids": [int(g) for g in a.gpus.split(",")] if a.gpus else None}
        if a.cmd == "load-model":
            body["extra_args"] = shlex.split(a.args)
        code, out = call("POST", "/v1/manager/models/" + ("fit" if a.cmd == "fit" else "load"), body)
        if a.json:
            print(json.dumps(out, indent=2))
        elif code == 200 and a.cmd == "fit":
            print_fit(out)
        elif code == 200:
            print(f"ready {out['profile']} on :{out['port']} GPU {out['gpus']} ctx {out['ctx']}"
                  + (f" split {out['tensor_split']}" if out.get("tensor_split") else ""))
            for c in (out.get("measured") or {}).get("cards", []):
                print(f"  GPU{c.get('gpu')} measured {json.dumps(c)}")
        else:
            err = out.get("error") or {}
            print(err.get("message", json.dumps(out)), file=sys.stderr)
        return EXIT.get(code, 1)

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
