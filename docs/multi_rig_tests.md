# Multi-rig tests

Tests for running more than one rig from the controller. Background: `DESIGN_NOTES.md`, "More than one
rig". The first two-rig setup is the GPU rig plus the Windows desktop worker (GTX 1070, llm_orchestration
`shared/scripts/desktop_worker/`).

## Runnable now (before the gateway is multi-rig)

`tools/multi_rig_tests.py` drives both rigs directly: the GPU rig through the harness, the desktop
through `ssh desktop-rig` and a tunnel. Same model on both (qwen2.5-coder:7b, ctx 8192).

```bash
# desktop: worker_on.cmd first
python3 tools/multi_rig_tests.py setup          # load on rig GPU 1 and the desktop, open the tunnel
python3 tools/multi_rig_tests.py disconnect     # 7 min; prompts when to unplug and replug the desktop
python3 tools/multi_rig_tests.py concurrency    # 1/2/4/8 streams per rig, 60 s each
tools/dual_rig_bench.sh                         # six custom benchmark tests on both rigs at once
python3 tools/multi_rig_tests.py teardown
```

| Test | What it answers | Watch in the summary |
|---|---|---|
| `disconnect` | What the controller sees when a rig drops off the network mid-load, and whether it recovers on its own | desktop first/last failure vs the prompts, `error_kinds` (timeouts vs refused), `longest_request_s` (how long a request hangs), rig lane `failed` (should be 0), tunnel restarts, first ok after replug, whether the desktop container survived |
| `concurrency` | Where the controller, the tunnels or a single-slot runtime become the limit | `gen_tok_per_s` flattening while `latency_p50_s` climbs (parallel 1 queues on the server) |
| `dual_rig_bench.sh` | Both rigs running a benchmark at once with the controller sampling both GPUs | identical scores, per-rig time, no failed GPU samples |

Defaults for `disconnect`: unplug prompt at +90 s, replug at +210 s, run ends at +420 s, probes every
5 s (`--unplug-at`, `--replug-at`, `--duration`). The test keeps its own tunnel with
`ServerAliveInterval=5`, `ServerAliveCountMax=3` and restarts it every 5 s while it is down. Output goes to
`~/rig_stress/multi_rig_<test>_<time>/`: `events.log`, `requests.csv`, `probes.csv`, `summary.json`.

Open question (2026-10-19): in each of three short `disconnect` runs with nothing unplugged, one desktop
request failed with `RemoteDisconnected` (the server finished it; the response was lost on the way back)
3-8 s after a short ping-loss window to the desktop. Not reproduced in four standalone runs through the
same tunnel (138 requests: alone, with 1/s health checks, across a tunnel restart, with continuous pings).
The 90 s before the unplug prompt is the baseline for it.

## Need the multi-rig gateway first

| Test | What it answers |
|---|---|
| Pull-queue throughput | A campaign split across GPU 0-5 plus the desktop, each rig claiming jobs that fit its free cards; total throughput vs the GPU rig alone |
| Disconnect with claimed work | The offline rule: a rig that drops finishes what it claimed and reports on reconnect; no hang, no double assignment |
| Placement by card class | Small, latency-sensitive jobs go to the 1070 and bulk work to the 1060s |
| Shared storage contention | Both rigs cold-loading from the Pi's NFS share over one gigabit link vs local disk on the desktop |

## Results

| Date | Test | Result |
|---|---|---|
| 2026-10-19 | dual_rig_bench (6 custom tests) | identical scores; rig 1060 3:06, desktop 1070 2:23; 18 of 18 GPU samples from both |
| 2026-10-19 | concurrency 1, 2 streams (20 s smoke) | rig 21.8 / 21.3 gen tok/s, desktop 25.3 / 25.6; p50 latency doubles at 2 streams (parallel 1 queues); 0 errors |
