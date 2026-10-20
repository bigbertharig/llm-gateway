"""multi_rig_tests: two-rig tests run from the controller, before the gateway is multi-rig.

    python3 tools/multi_rig_tests.py setup          load the test model on both rigs, open the desktop tunnel
    python3 tools/multi_rig_tests.py disconnect     steady load on both rigs; unplug the desktop when told
    python3 tools/multi_rig_tests.py concurrency    1/2/4/8 parallel streams to both rigs
    python3 tools/multi_rig_tests.py teardown       unload both, close the tunnel

Rigs: the GPU rig through the harness (rigctl, port 11435 via pi-rig-tunnel) and the desktop worker
(`ssh desktop-rig`, container on its port 11434, tunnelled here to 11450). The desktop must be in
worker mode (llm_orchestration shared/scripts/desktop_worker/). Standard library only.
Model: the benchmarking role single_1060 by default (`--role ROLE` or `--model ID` before the test
name); the desktop needs that model's GGUF under ~/models/ (setup prints the copy command if not).
Output: ~/rig_stress/multi_rig_<test>_<time>/ (events.log, requests.csv, probes.csv, summary.json).
See docs/multi_rig_tests.md.
"""
import argparse, base64, csv, json, re, statistics, subprocess, sys, threading, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import role_model  # noqa: E402

RIGCTL = [sys.executable, str(HERE / "rigctl.py")]
RIG_GPU, RIG_PORT, RIG_HOST = "1", 11435, "10.0.0.3"
DESK_SSH, DESK_HOST, DESK_REMOTE_PORT, DESK_PORT = "desktop-rig", "10.0.0.4", 11434, 11450
DESK_MODELS = "/home/bryan/models"
LANES = {"rig": RIG_PORT, "desktop": DESK_PORT}
# Set by pick_model() from --role / --model.
MODEL = DESK_GGUF = DESK_CONTAINER = None


def pick_model(role, model):
    global MODEL, DESK_GGUF, DESK_CONTAINER
    info = role_model.resolve_model(model) if model else role_model.resolve(role)
    MODEL = info["model"]
    DESK_GGUF = f"{DESK_MODELS}/{info['gguf_relative']}"
    DESK_CONTAINER = "desk-" + re.sub(r"[^a-z0-9.-]+", "-", MODEL.lower())


class Run:
    def __init__(self, test):
        self.out = Path.home() / f"rig_stress/multi_rig_{test}_{time.strftime('%Y%m%d_%H%M%S')}"
        self.out.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.t0 = time.time()

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} +{time.time() - self.t0:5.0f}s {msg}"
        print(line, flush=True)
        with self.lock, open(self.out / "events.log", "a") as f:
            f.write(line + "\n")

    def row(self, name, fields, values):
        p = self.out / name
        with self.lock:
            new = not p.exists()
            with open(p, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(fields)
                w.writerow(values)


def health(port, timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def reach(host, timeout=1):
    # ICMP, not a TCP connect to port 22: the desktop's Windows sshd starts a process per connection,
    # and connecting every second disturbs it (seen as dropped tunnel requests).
    # Two packets: Windows drops a single echo now and then while the link is fine.
    return subprocess.run(["ping", "-c", "2", "-i", "0.2", "-W", str(timeout), host],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def chat(port, max_tokens=128, timeout=60):
    body = json.dumps({"messages": [{"role": "user", "content":
                       f"Run {time.time_ns()}. Explain one idea from distributed systems in detail."}],
                       "max_tokens": max_tokens, "temperature": 0.7}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["timings"]


def ssh(host, cmd, timeout=60):
    # The desktop's sshd is Windows OpenSSH handing the command to bash.exe; Windows command-line
    # quoting mangles redirects and $(...), so the command travels base64-encoded.
    wire = f"echo {base64.b64encode(cmd.encode()).decode()} | base64 -d | bash"
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, wire],
                       capture_output=True, text=True, timeout=timeout)
    out = "\n".join(l for l in (p.stdout + p.stderr).splitlines() if "systemd user session" not in l)
    return p.returncode, out.strip()


# ---------------------------------------------------------------- tunnel

class Tunnel(threading.Thread):
    """Holds ssh -L to the desktop and restarts it whenever it drops; logs each drop and restart."""

    def __init__(self, run):
        super().__init__(daemon=True)
        self.run_, self.restarts, self.proc = run, 0, None

    def run(self):
        while not self.run_.stop.is_set():
            self.proc = subprocess.Popen(
                ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=5",
                 "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
                 "-L", f"{DESK_PORT}:127.0.0.1:{DESK_REMOTE_PORT}", DESK_SSH],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            started = time.time()
            while self.proc.poll() is None and not self.run_.stop.is_set():
                time.sleep(0.5)
            if self.run_.stop.is_set():
                self.proc.terminate()
                break
            err = (self.proc.stderr.read() or "").strip().splitlines()
            self.restarts += 1
            self.run_.log(f"tunnel down after {time.time() - started:.0f}s (exit {self.proc.returncode}"
                          f"{': ' + err[-1] if err else ''}); retrying in 5 s")
            self.run_.stop.wait(5)


def free_port(port):
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f":{port} " in line and "pid=" in line:
            pid = line.split("pid=")[1].split(",")[0]
            subprocess.run(["kill", pid])
            return pid
    return None


# ---------------------------------------------------------------- setup / teardown

def serving_gguf(port):
    """Basename of the GGUF a local port serves, or None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
            data = json.loads(r.read())
        return Path((data.get("models") or data.get("data") or [{}])[0].get("model")
                    or data["data"][0]["id"]).name
    except Exception:
        return None


def unload_rig_port():
    st = subprocess.run(RIGCTL + ["status", "--json"], capture_output=True, text=True, timeout=60)
    try:
        for pid, prof in (json.loads(st.stdout).get("profiles") or {}).items():
            if prof.get("port") == RIG_PORT:
                subprocess.run(RIGCTL + ["unload", pid], capture_output=True, timeout=300)
                print("rig: unloaded", pid)
    except (ValueError, AttributeError):
        print("rig: could not read status; unload by hand with rigctl unload")


def setup(_args):
    want = Path(DESK_GGUF).name
    print(f"model: {MODEL} ({want})")
    if health(RIG_PORT) and serving_gguf(RIG_PORT) != want:
        print(f"rig: port {RIG_PORT} serves {serving_gguf(RIG_PORT)}, replacing it")
        unload_rig_port()
    if not health(RIG_PORT):
        p = subprocess.run(RIGCTL + ["load-model", MODEL, "--gpus", RIG_GPU, "--ctx", "8192", "--use", "benchmark",
                                     "--json"], capture_output=True, text=True, timeout=900)
        if p.returncode == 0:
            print("rig:", json.loads(p.stdout).get("state"))
        else:  # rigctl explains on stdout (for example a plan held after a rig reboot)
            print(f"rig load failed (rc {p.returncode}):", (p.stdout + p.stderr).strip()[-400:])
    print("rig health:", health(RIG_PORT), serving_gguf(RIG_PORT))
    rc, out = ssh(DESK_SSH, "docker info >/dev/null 2>&1 && echo up")
    if "up" not in out:
        sys.exit("desktop: Docker is not up; switch the desktop to worker mode (worker_on.cmd) first")
    rc, out = ssh(DESK_SSH, f"test -f {DESK_GGUF} && echo present")
    if "present" not in out:
        rel = DESK_GGUF[len(DESK_MODELS) + 1:]
        sys.exit(f"desktop: {DESK_GGUF} is missing. Copy it from the Pi share, e.g. on the Pi:\n"
                 f"  ssh desktop-rig 'mkdir -p {Path(DESK_GGUF).parent}' && "
                 f"cat /media/bryan/shared/models/{rel} | ssh desktop-rig 'cat > {DESK_GGUF}'")
    rc, out = ssh(DESK_SSH, f"docker ps --filter name=^{DESK_CONTAINER}$ -q | grep -q . && "
                            f"curl -sf http://127.0.0.1:{DESK_REMOTE_PORT}/health >/dev/null && echo serving")
    if "serving" not in out:
        cmd = (f"docker ps -aq --filter name=^desk- | xargs -r docker rm -f >/dev/null 2>&1; "
               f"docker run --detach --name {DESK_CONTAINER} "
               f"--gpus device=0 --oom-score-adj 500 --network host -v $(dirname {DESK_GGUF}):$(dirname {DESK_GGUF}):ro "
               f"llama-runtime:b11669 llama-server --model {DESK_GGUF} --host 127.0.0.1 --port {DESK_REMOTE_PORT} "
               f"--n-gpu-layers 999 --batch-size 64 --parallel 1 --flash-attn on -ctk q8_0 -ctv q8_0 "
               f"--ctx-size 8192 -lv 4 >/dev/null; for i in $(seq 60); do "
               f"curl -sf http://127.0.0.1:{DESK_REMOTE_PORT}/health >/dev/null && echo serving && break; sleep 3; done")
        rc, out = ssh(DESK_SSH, cmd, timeout=240)
    print("desktop runtime:", out or "not serving")
    print("desktop health via tunnel:", ensure_tunnel())


def ensure_tunnel():
    """Background ssh -L to the desktop (the tests between runs); True when the desktop serves through it."""
    if not health(DESK_PORT):
        free_port(DESK_PORT)
        subprocess.run(["ssh", "-f", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                        "-o", "ServerAliveInterval=15", "-L", f"{DESK_PORT}:127.0.0.1:{DESK_REMOTE_PORT}", DESK_SSH])
        time.sleep(1)
    return health(DESK_PORT)


def teardown(_args):
    unload_rig_port()
    rc, out = ssh(DESK_SSH, "docker ps -aq --filter name=^desk- | xargs -r docker rm -f >/dev/null 2>&1 && echo removed")
    print("desktop container:", out or f"not reachable (rc {rc})")
    print("tunnel: closed pid", free_port(DESK_PORT))


# ---------------------------------------------------------------- disconnect

def lane_loop(run, name, port, max_tokens):
    while not run.stop.is_set():
        s = time.time()
        try:
            t = chat(port, max_tokens=max_tokens)
            ok, err, tps = 1, "", round(t.get("predicted_per_second", 0), 1)
        except Exception as e:
            ok, err, tps = 0, f"{type(e).__name__}: {str(e)[:80]}", 0
        run.row("requests.csv", ["time", "elapsed_s", "lane", "ok", "seconds", "decode_tps", "error"],
                [time.strftime('%H:%M:%S'), round(s - run.t0, 1), name, ok, round(time.time() - s, 1), tps, err])
        if not ok:
            run.stop.wait(2)


def probe_loop(run, every):
    while not run.stop.is_set():
        run.row("probes.csv", ["time", "elapsed_s", "rig_ping", "desk_ping", "rig_health", "desk_health"],
                [time.strftime('%H:%M:%S'), round(time.time() - run.t0, 1), int(reach(RIG_HOST)),
                 int(reach(DESK_HOST)), int(health(RIG_PORT)), int(health(DESK_PORT))])
        run.stop.wait(every)


def summarize_disconnect(run, unplug_at, replug_at, tunnel):
    reqs = list(csv.DictReader(open(run.out / "requests.csv")))
    probes = list(csv.DictReader(open(run.out / "probes.csv")))
    s = {"unplug_prompt_s": unplug_at, "replug_prompt_s": replug_at, "tunnel_restarts": tunnel.restarts}
    for lane in LANES:
        r = [x for x in reqs if x["lane"] == lane]
        bad = [x for x in r if x["ok"] == "0"]
        s[lane] = {"requests": len(r), "ok": len(r) - len(bad), "failed": len(bad),
                   "first_failure_s": float(bad[0]["elapsed_s"]) if bad else None,
                   "last_failure_s": float(bad[-1]["elapsed_s"]) if bad else None,
                   "longest_request_s": max((float(x["seconds"]) for x in r), default=0),
                   "error_kinds": sorted({x["error"].split(":")[0] for x in bad})}
        good_after = [x for x in r if x["ok"] == "1" and bad and float(x["elapsed_s"]) > float(bad[-1]["elapsed_s"])]
        s[lane]["first_ok_after_failures_s"] = float(good_after[0]["elapsed_s"]) if good_after else None
    windows, start, prev = [], None, None  # contiguous runs of probes with no ping reply
    for p in probes:
        t, down = float(p["elapsed_s"]), p["desk_ping"] == "0"
        if down and start is None:
            start = t
        if not down and start is not None:
            windows.append([start, prev])
            start = None
        prev = t
    if start is not None:
        windows.append([start, prev])
    s["desktop_unreachable_windows_s"] = windows
    s["rig_unreachable_samples"] = sum(1 for p in probes if p["rig_ping"] == "0" or p["rig_health"] == "0")
    rc, out = ssh(DESK_SSH, f"docker ps --filter name={DESK_CONTAINER} --format '{{{{.Status}}}}'")
    s["desktop_container_after"] = out or f"unreachable (rc {rc})"
    (run.out / "summary.json").write_text(json.dumps(s, indent=1))
    return s


def disconnect(args):
    run = Run("disconnect")
    if not (health(RIG_PORT) and (health(DESK_PORT) or health(DESK_PORT, 5))):
        sys.exit("both lanes must be serving first: run `setup`")
    free_port(DESK_PORT)  # the test holds its own reconnecting tunnel
    tunnel = Tunnel(run)
    tunnel.start()
    time.sleep(2)
    run.log(f"start: {args.duration}s; unplug the desktop's Ethernet at +{args.unplug_at}s, "
            f"plug it back in at +{args.replug_at}s; out {run.out}")
    threads = [threading.Thread(target=probe_loop, args=(run, args.probe_every), daemon=True)]
    threads += [threading.Thread(target=lane_loop, args=(run, n, p, args.max_tokens), daemon=True) for n, p in LANES.items()]
    for t in threads:
        t.start()
    prompts = {args.unplug_at - 10: "unplug in 10 s", args.unplug_at: ">>> UNPLUG the desktop Ethernet now <<<",
               args.replug_at - 10: "plug back in in 10 s", args.replug_at: ">>> PLUG the desktop Ethernet back in <<<"}
    last_state = None
    while time.time() - run.t0 < args.duration:
        el = time.time() - run.t0
        for at in sorted(k for k in prompts if k <= el):
            run.log(prompts.pop(at))
        state = (reach(DESK_HOST), health(DESK_PORT, timeout=1))
        if state != last_state:
            run.log(f"desktop ping {'up' if state[0] else 'DOWN'}, model {'serving' if state[1] else 'NOT serving'}")
            last_state = state
        time.sleep(1)
    run.stop.set()
    for t in threads:
        t.join(timeout=70)
    s = summarize_disconnect(run, args.unplug_at, args.replug_at, tunnel)
    run.log("summary: " + json.dumps(s))
    run.log(f"background tunnel for the next test: {'up' if ensure_tunnel() else 'DOWN'}")


# ---------------------------------------------------------------- concurrency

def concurrency(args):
    if not (health(RIG_PORT) and ensure_tunnel()):
        sys.exit("both lanes must be serving first: run `setup`")
    run = Run("concurrency")
    results = []
    for c in args.levels:
        run.log(f"level {c}: {c} streams per rig for {args.seconds}s")
        lat = {n: [] for n in LANES}
        toks = {n: 0 for n in LANES}
        errs = {n: 0 for n in LANES}
        end = time.time() + args.seconds

        def stream(name, port):
            while time.time() < end:
                s = time.time()
                try:
                    t = chat(port, max_tokens=args.max_tokens, timeout=120)
                    with run.lock:
                        lat[name].append(time.time() - s)
                        toks[name] += t.get("predicted_n", 0)
                except Exception:
                    with run.lock:
                        errs[name] += 1
                    time.sleep(1)

        ts = [threading.Thread(target=stream, args=(n, p)) for n, p in LANES.items() for _ in range(c)]
        t0 = time.time()
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        wall = time.time() - t0
        row = {"streams_per_rig": c}
        for n in LANES:
            l = sorted(lat[n])
            row[n] = {"requests": len(l), "errors": errs[n], "gen_tok_per_s": round(toks[n] / wall, 1),
                      "latency_p50_s": round(statistics.median(l), 2) if l else None,
                      "latency_p95_s": round(l[int(0.95 * (len(l) - 1))], 2) if l else None}
        results.append(row)
        run.log(json.dumps(row))
    (run.out / "summary.json").write_text(json.dumps(results, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", default=role_model.DEFAULT_ROLE, help="benchmarking role for the test model")
    ap.add_argument("--model", help="model id from the registry; overrides --role")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("teardown")
    d = sub.add_parser("disconnect")
    d.add_argument("--duration", type=int, default=420)
    d.add_argument("--unplug-at", type=int, default=90)
    d.add_argument("--replug-at", type=int, default=210)
    d.add_argument("--probe-every", type=float, default=5)
    d.add_argument("--max-tokens", type=int, default=128)
    c = sub.add_parser("concurrency")
    c.add_argument("--levels", type=int, nargs="+", default=[1, 2, 4, 8])
    c.add_argument("--seconds", type=int, default=60)
    c.add_argument("--max-tokens", type=int, default=128)
    args = ap.parse_args()
    pick_model(args.role, args.model)
    {"setup": setup, "teardown": teardown, "disconnect": disconnect, "concurrency": concurrency}[args.cmd](args)


if __name__ == "__main__":
    main()
