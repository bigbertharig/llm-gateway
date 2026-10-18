"""Check rig profiles through the gateway: what an agent harness (Roo) actually depends on.

Talks only to the gateway HTTP API, so it runs anywhere the gateway is reachable
(on the Pi: http://127.0.0.1:8080; elsewhere: forward the Pi's 8080).

    .venv/bin/python tools/model_check.py --profiles coder-7b chat-27b
    .venv/bin/python tools/model_check.py --profiles coder-30b --exclusive   # unload others first (rig over ssh)

Per profile: load time, advertised vs served context, thinking leak, streaming, tool calling,
/tokenize, prompt-processing speed at growing prompt sizes, and the context-overflow error.
Writes a JSON report to results/ and prints a summary table.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

FILLER = ("The rig loads one model per profile and serves it over an OpenAI compatible API. "
          "Harnesses send the whole conversation every turn, so prompt processing speed matters. ")
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}


class Check:
    def __init__(self, gateway: str, timeout: float):
        self.http = httpx.Client(base_url=gateway.rstrip("/"), timeout=timeout)

    def chat(self, model: str, messages: list, **extra) -> tuple[int, dict, float]:
        t0 = time.monotonic()
        r = self.http.post("/v1/chat/completions", json={"model": model, "messages": messages, **extra})
        dt = time.monotonic() - t0
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:300]}
        return r.status_code, body, dt

    def tokens(self, model: str, text: str) -> int:
        r = self.http.post("/tokenize", json={"model": model, "content": text})
        r.raise_for_status()
        return len(r.json()["tokens"])

    def prompt_of(self, model: str, n_tokens: int) -> str:
        """A prompt of ~n_tokens; unique prefix so llama-server's prompt cache can't help."""
        per_unit = self.tokens(model, FILLER)
        return f"[{uuid.uuid4()}] " + FILLER * max(1, n_tokens // per_unit)


def run_profile(c: Check, model: str, ctx: int) -> dict:
    res: dict = {"model": model, "advertised_ctx": ctx}
    msg = [{"role": "user", "content": "Reply with exactly: ok"}]

    status, body, dt = c.chat(model, msg, max_tokens=20)
    res["first_request_s"] = round(dt, 1)  # includes load if the profile was cold
    if status != 200:
        res["error"] = f"first request {status}: {json.dumps(body)[:400]}"
        return res
    m = body["choices"][0]["message"]
    res["reply"] = (m.get("content") or "").strip()[:40]
    res["thinking_leak"] = bool(m.get("reasoning_content")) or "<think>" in (m.get("content") or "")

    served = next((p for p in c.http.get("/v1/rig/status").json().get("profiles", [])
                   if f"gpu-rig/{p['id']}" == model), None)
    res["served_ctx"] = served and served.get("ctx_size")
    res["gpus"] = served and served.get("gpus")
    res["ctx_matches"] = res["served_ctx"] == ctx

    with c.http.stream("POST", "/v1/chat/completions", json={
            "model": model, "stream": True, "max_tokens": 20,
            "messages": [{"role": "user", "content": "Count from 1 to 5."}]}) as r:
        events = [l for l in r.iter_lines() if l.startswith("data:")]
    res["stream_ok"] = r.status_code == 200 and bool(events) and events[-1].strip() == "data: [DONE]"

    status, body, _ = c.chat(model, [{"role": "user", "content": "What is the weather in Paris right now?"}],
                             tools=[WEATHER_TOOL], tool_choice="auto", max_tokens=200)
    calls = (body.get("choices") or [{}])[0].get("message", {}).get("tool_calls") or [] if status == 200 else []
    try:
        args = json.loads(calls[0]["function"]["arguments"]) if calls else {}
    except (ValueError, KeyError, TypeError):
        args = {}
    res["tool_call_ok"] = bool(calls) and calls[0]["function"]["name"] == "get_weather" and "paris" in str(args).lower()
    if not res["tool_call_ok"]:
        res["tool_call_detail"] = f"{status}: {json.dumps(body)[:300]}"

    res["tokenize_ok"] = c.tokens(model, "hello world") > 0

    speeds = {}
    for n in sorted({2048, 8192, ctx // 2, int(ctx * 0.85)}):
        if n >= ctx:
            continue
        status, body, dt = c.chat(model, [{"role": "user", "content": c.prompt_of(model, n) + "\nSay ok."}],
                                  max_tokens=8)
        t = body.get("timings", {}) if status == 200 else {}
        speeds[n] = {"status": status, "wall_s": round(dt, 1),
                     "prompt_n": t.get("prompt_n"), "prompt_tps": round(t.get("prompt_per_second", 0), 1),
                     "gen_tps": round(t.get("predicted_per_second", 0), 1)}
    res["prompt_speed"] = speeds

    status, body, _ = c.chat(model, [{"role": "user", "content": c.prompt_of(model, int(ctx * 1.1))}], max_tokens=8)
    err = body.get("error", {}) if isinstance(body, dict) else {}
    res["overflow_ok"] = status == 400 and err.get("code") == "context_length_exceeded"
    return res


def unload_others(rig_command: list[str], keep: str) -> None:
    st = json.loads(subprocess.run([*rig_command, "status", "--json"], capture_output=True,
                                   text=True, check=True).stdout)
    others = [p["id"] for p in st.get("profiles", []) if p["id"] != keep]
    if others:
        print(f"  unloading {others} to make room for {keep}", flush=True)
        subprocess.run([*rig_command, "stop", *others], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--gateway", default="http://127.0.0.1:8080")
    ap.add_argument("--profiles", nargs="*", help="profile ids (default: all published)")
    ap.add_argument("--exclusive", action="store_true",
                    help="before each profile, unload every other loaded profile (needs --rig-command)")
    ap.add_argument("--rig-command", default="ssh -o BatchMode=yes 10.0.0.3 /usr/local/bin/rig")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "results"))
    args = ap.parse_args()

    c = Check(args.gateway, args.timeout)
    published = {m["id"].split("/", 1)[1]: m for m in c.http.get("/v1/models").json()["data"]}
    ids = args.profiles or list(published)
    unknown = [i for i in ids if i not in published]
    if unknown:
        sys.exit(f"unknown profiles {unknown}; published: {sorted(published)}")

    results = []
    for pid in ids:
        print(f"== {pid}", flush=True)
        if args.exclusive:
            unload_others(shlex.split(args.rig_command), pid)
        try:
            r = run_profile(c, f"gpu-rig/{pid}", published[pid]["context_length"])
        except Exception as exc:  # report and keep going with the next profile
            r = {"model": f"gpu-rig/{pid}", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(r, indent=2), flush=True)
        results.append(r)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"model_check_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"gateway": args.gateway, "results": results}, indent=2) + "\n")

    print(f"\n{'profile':<22}{'ctx':>7} {'load':>6} {'think':>6} {'strm':>5} {'tool':>5} {'ovfl':>5}  prompt tok/s @ size")
    for r in results:
        if "error" in r and "reply" not in r:
            print(f"{r['model']:<22} ERROR {r['error'][:90]}")
            continue
        sp = "  ".join(f"{n // 1024}k:{v['prompt_tps']}" for n, v in r["prompt_speed"].items())
        yn = lambda b: "ok" if b else "NO"
        print(f"{r['model']:<22}{r['served_ctx'] or '?':>7} {r['first_request_s']:>5}s {'LEAK' if r['thinking_leak'] else 'no':>6}"
              f" {yn(r['stream_ok']):>5} {yn(r['tool_call_ok']):>5} {yn(r['overflow_ok']):>5}  {sp}")
    print(f"\nreport: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
