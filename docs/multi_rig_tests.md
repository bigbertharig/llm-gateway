# Multi-rig tests

Tests for running more than one rig from the controller. Background: `DESIGN_NOTES.md`, "More than one
rig". The first two-rig setup is the GPU rig plus the Windows desktop worker (GTX 1070, llm_orchestration
`shared/scripts/desktop_worker/`).

## Runnable now (before the gateway is multi-rig)

`tools/multi_rig_tests.py` drives both rigs directly: the GPU rig through the harness, the desktop
through `ssh desktop-rig` and a tunnel. Same model on both, ctx 8192: the benchmarking role
`single_1060` by default (`tools/role_model.py`, which reads model_roles.json; ornith-1.5:9b since
2026-10-20, qwen2.5-coder:7b before). `--role ROLE` or `--model ID` picks another; the 2026-10-19
results below used qwen2.5-coder:7b.

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
The 90 s before the unplug prompt is the baseline for it. In the full disconnect run the desktop missed
both of two pings or a 1 s health check about every 20-40 s with the cable in, and one request failed the
same way (+417 s); the rig never did. Next suspect: the desktop's network adapter or Windows side, not the
tunnel.

## Need the multi-rig gateway first

| Test | What it answers |
|---|---|
| Pull-queue throughput | A campaign split across GPU 0-5 plus the desktop, each rig claiming jobs that fit its free cards; total throughput vs the GPU rig alone |
| Disconnect with claimed work | The offline rule: a rig that drops finishes what it claimed and reports on reconnect; no hang, no double assignment |
| Placement by card class | Small, latency-sensitive jobs go to the 1070 and bulk work to the 1060s |
| Shared storage contention | Both rigs cold-loading from the Pi's NFS share over one gigabit link vs local disk on the desktop |

## Cross-machine split over the network (llama.cpp RPC), planned

Question: can one model split across the GPU rig and the desktop 1070 over gigabit without losing decode
speed? If yes, two rigs act as one VRAM pool for models too big for either (a duplicate rig = about 160 GB
for MoE); if no, each rig stays at its own VRAM. Not run yet (designed 2026-10-20).

Why it might work: the 2026-10-19 PCIe sweep (benchmarking `BENCHMARK_LESSONS_LEARNED.md`, "PCIe
generation and split scaling") measured 6-58 MB/s per card link during split inference; gigabit carries
about 117 MB/s. Decode speed was set by each card's VRAM, not by the link. Unknown: per-hop latency over
the network, llama.cpp RPC overhead, prefill (batched activations are much larger than decode), and load
time (weights are sent to the RPC server unless it caches them). The harness research lists "too slow over
gigabit" as a lesson, but that was never measured.

### Setup

1. **Network first.** All three machines are on the TL-SG105. Run `iperf3` rig <-> desktop both ways
   (expect about 940 Mbit/s) and a 10-minute `ping -i 0.2` from the rig. The 2026-10-19 disconnect test
   saw the desktop fail a health check every 20-40 s with the cable in (suspect: the desktop's adapter or
   the Windows side); a split runtime dies with its link, so fix or rule this out before the RPC test.
2. **Desktop on:** `setup_desktop_rig_ssh.ps1` (re-enables sshd and the RigNet rules), `worker_on.cmd`.
3. **RPC builds, same llama.cpp commit on both sides** (the RPC protocol is versioned): build
   `llama-runtime:b11669-rpc` on the rig from `shared/scripts/llama_runtime/Dockerfile` with
   `-DGGML_RPC=ON` added, and the same image in desktop WSL (sm_61 covers the 1070). Today's image has no
   RPC backend.
4. **Reach the desktop's rpc-server.** WSL2 is NAT'd: either switch WSL to mirrored networking
   (`.wslconfig`: `networkingMode=mirrored`) so the port binds on the LAN, or tunnel with `ssh -L` from the
   rig. Prefer mirrored: a tunnel adds its own overhead to every hop. Measure both if mirrored is not
   possible.
5. **Lock it down.** `rpc-server` has no authentication and llama.cpp warns never to expose it. Bind it to
   the desktop's Ethernet address and add a Windows firewall rule like the RigNet ones: inbound TCP on its
   port, remote address 10.0.0.3 only, interface Ethernet. Remove the rule and stop the server after the
   test (`teardown_desktop_rig_ssh.ps1 -Level pause` for ssh).
6. **Cards:** hold rig cards with `rigctl claim` for the test's duration so lanes and campaigns cannot
   take them. Loads go outside the harness (it does not know RPC devices), as in the 2026-10-19 tests.

### Runs

Same method as the PCIe sweep so the numbers compare: decode = median of 3 x 256 tokens; prefill = median
of 3 unique ~3.7k-token prompts at `-ub 64` and `-ub 512`; ctx 8192; also record cold load time with and
without the rpc-server cache (`-c`), rig NIC bytes per generated token (`/proc/net/dev` before and after),
errors.

| Run | Model | Cards | Compare with |
|---|---|---|---|
| A | qwen2.5-coder:7b | rig GPU 1 alone | sweep: 22.3 tok/s |
| B | qwen2.5-coder:7b | desktop 1070 alone, through RPC from the rig | A, and the desktop's native 25.3 tok/s (concurrency test) |
| C | qwen2.5-coder:7b | rig GPU 1 + desktop 1070 (RPC) | local split GPU 1+2: 23.6 tok/s |
| D | ministral-3:14b | rig GPU 1 + desktop 1070 (RPC) | local 4+5 split (2026-10-20 lane, 13.5 tok/s) |
| E | an MoE that needs both, e.g. gemma-4:26b-a4b | rig GPUs 1-3 + desktop 1070 | the 4-way 1060 split (24.2 tok/s) |
| F | run C, with the desktop's cable pulled for 30 s | | the failure mode: does the runtime hang, error, or recover |

### Reading the result

- Decode in C-E within card-to-card variation of the local split: the network is not the limit for decode.
- Decode clearly lower: compare bytes per token x tok/s with the link capacity. Near the link's limit means
  a faster switch and NICs (2.5 or 10 GbE) would help; far below it means latency or RPC overhead, which a
  faster switch would not fix.
- Prefill matters for long chats: report it separately; a split that decodes fine but prefills slowly is
  still usable for chat with prompt caching, not for long fresh prompts.
- Record results in the table below and a short entry in `DESIGN_NOTES.md`, "More than one rig".

## Results

| Date | Test | Result |
|---|---|---|
| 2026-10-19 | dual_rig_bench (6 custom tests) | identical scores; rig 1060 3:06, desktop 1070 2:23; 18 of 18 GPU samples from both |
| 2026-10-19 | disconnect (desktop Ethernet out about +87 to +216 s) | rig 73/73 ok throughout. Desktop: 18/18 before; the in-flight request hung 20.5 s then `RemoteDisconnected`; every request during the outage failed at once (`Connection refused`, the tunnel listener was gone), so nothing hung. The tunnel retried 16 times (`No route to host`) and reconnected by itself; first ok 5 s after the cable was back (2 failures at reconnect), then 36/37 ok. The desktop container stayed up (worker mode held WSL) |
| 2026-10-19 | concurrency 1, 2 streams (20 s smoke) | rig 21.8 / 21.3 gen tok/s, desktop 25.3 / 25.6; p50 latency doubles at 2 streams (parallel 1 queues); 0 errors |
