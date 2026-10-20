# Harness design notes

Running notes for the manager design (`MANAGER.md` is the process; this file is the knowledge the
manager has to balance). Started 2026-10-17.

## Host RAM is the scarce resource, not VRAM

The rig has 31.4 GB of system RAM (32 GB max on this board) against 24 + 5x6 GB of VRAM today, and
a planned 24 + 8x8 GB (88 GB). Fully offloaded models still use host RAM, and RAM is what failed
first: on 2026-10-17 earlyoom killed a benchmark lane's runtime at 21:41 (run notes:
`logs/benchmarks/worker_lanes_20261017_203028/RUN_NOTES.md`, sections 1, 2, 9).

Measured 2026-10-17 ~22:00, five fully offloaded workers on single 1060s, all on llama.cpp's default
`--cache-ram` (8192 MiB host prompt cache per server), anonymous memory + swap per llama-server:

| Server | Uptime | Host RAM |
|---|---|---|
| Ministral-3-8B, ctx 16k | 88 min | 8.5 GB |
| Qwen2.5-Coder-7B, ctx 16k | 88 min | 4.8 GB |
| Gemma-4-e4b | 87 min | 4.2 GB |
| Llama-3.2-3B | 17 min | 3.3 GB |
| Gemma-4-e2b | 68 min | 1.6 GB |

About 22 GB in total, 8 GB of it in swap; `free` showed 6.4 GB available. Mostly prompt cache,
growing with uptime: Ministral reported 7209 MiB of cache (run notes), so its base was roughly
1.3 GB. That is one data point; the base footprint is not measured otherwise.

What follows from it:
- Host RAM scales with the number of runtimes and their cache caps, not with VRAM. A split model
  costs host RAM once per server, not per GPU (plus a CUDA context per device, not measured).
- Five runtimes on the default cache can reach 5 x (8 GB + base), more than the board holds. With
  the cache off or small, five runtimes are roughly 5 x base.
- Large context costs VRAM (KV on the GPUs), but the prompt cache saves past prompt states in host
  RAM; at 100k+ ctx one saved state can be several GB, so big-context profiles need it capped or off.
- Other host-RAM users to budget: Gemma-4 per-layer embeddings (2.2-2.4 GB, host by design),
  benchmark evaluation containers (EvalPlus peaked at 4.7 GB), OS and Docker, and earlyoom's kill
  line (about 10% available, and swap free below 50%).
- Base footprint, first direct reading (2026-10-17 23:24): gpt-oss-20b on the 3090, fully offloaded,
  just loaded with an empty cache: 269 MB host RAM (anonymous + swap, read by the rig guard).
- To measure before fixing numbers: one server's footprint with `--cache-ram 0` after an hour of
  benchmark traffic, and per-device CUDA context cost for a 5-way split.

### Policy by use (operator direction 2026-10-17)

| Use | Prompt cache | Why |
|---|---|---|
| Orchestration workers | off (`--cache-ram 0`) | mostly one-shot system prompts and tasks |
| Benchmarking | off | independent prompts, prefix reuse near zero (lcp 36 tokens seen) |
| Offline conversations and coding sessions | on, bounded (build later) | multi-turn reuse is real; return to an earlier conversation reuses its prefix |

The manager has to know what is running and what it is about to load, and set the cache (and ctx,
slots, KV type) per use at load time: registry settings per model merged with a use profile.
Built 2026-10-17 for the cache: `run_runtime.sh` floors `--cache-ram` at 0; each offered profile has
a `use` (`OFFER` in `tools/generate_profiles.py`); `manager/memory.py` picks the cache per load from
the use (chat 4096 MiB, worker and benchmark 0) and the rig's available RAM minus an 8 GB reserve,
and passes it on the launch command. ctx, slots and KV type per use are still open. The
rig guard enforces a RAM budget at admission: each runtime costs base + its cache cap + known
host-resident parts, and a load is admitted only if the total stays under RAM minus a reserve.

In practice one use runs at a time (workers, or benchmarks, or chat), even though the systems are
built to run side by side later. The defaults should make the single-use case simple and safe.

## Layout options seen so far

- Three brain-class models (3090; two sets of 3x 1070) plus two single 1070 workers: five runtimes,
  the same count as the 2026-10-17 lanes. Fine for RAM with the cache off; overflows with defaults.
- The 3090 plus every 1070 split under one model: one runtime, the least host RAM, the most context.
- Single bigger models: few runtimes, RAM is not the limit.

## More than one rig

- Each rig reports itself: `rig observe` (rig-side, llm_orchestration `scripts/rig/`) already carries
  GPUs, containers, ports, jobs, the guard and the rig's own lease/claim record. A second rig runs
  the same code; the controller reads one report per rig.
- Each rig keeps its own lease record, so nothing has to merge across rigs. The controller keeps a
  rig list (name, ssh target) and places across all of them.
- Two ways to send work, likely both:
  - push: the controller picks rig and card and sends the command. Needed for interactive use
    (a waiting request cannot sit in a queue).
  - pull: jobs go into a queue and each rig claims the ones that fit its free cards. Fits batch work
    (benchmarks, orchestration batches) and the offline rule: a disconnected rig finishes what it
    claimed and holds the results until it reconnects.
- Profile ids should stay rig-agnostic (`gpu-rig/<profile>`), with the rig chosen by placement.

### First two-rig test (2026-10-19)

A Windows desktop (GTX 1070 8GB, 10.0.0.4) ran as a second rig next to the GPU rig, both driven from
the Pi. The gateway is still single-rig, so the desktop side was hand-started: a container plus a
Pi tunnel, not `rig.py`.

- Access: Windows OpenSSH server with its default shell set to WSL `bash.exe`, so `ssh desktop-rig`
  lands in Ubuntu like `ssh rig`. Key-only, firewall open to 10.0.0.0/24 on the Ethernet adapter
  only. Windows 10 has no WSL mirrored networking, so sshd inside WSL is not reachable.
- Worker mode is a manual switch (`worker_mode.ps1 on|off|status`, llm_orchestration
  `shared/scripts/desktop_worker/`). WSL stops the Ubuntu instance when no Windows process holds
  it, which kills Docker and the containers; `on` starts a hidden keepalive. Nothing auto-starts.
  After `on`, Docker takes about 40 s.
- Model ports: `ssh -L <pi-port>:127.0.0.1:<port> desktop-rig` works, through WSL's localhost
  forwarding, as long as worker mode is on.
- Driver: WSL uses the Windows driver. The CUDA 12.9 runtime image needs the 580 branch there too
  (Windows 582.78 for Pascal, same branch as the rig's Linux 580.159.03).
- Result: the Pi ran the six custom tests on rig GPU 1 (1060) and the desktop 1070 at once, both
  qwen2.5-coder:7b, while sampling both GPUs every 10 s (no failed samples). Identical scores;
  the desktop finished in 2:23 vs 3:06 (decode about 30.7 vs 23 tok/s).
  Tool: `tools/dual_rig_bench.sh`.
- Still needed for one controller: a rig list in settings, per-rig observe and tunnels, cross-rig
  placement (splits stay within a rig), and `rig.py` on the desktop so the gateway can load there.
- Disconnect test (`docs/multi_rig_tests.md`): the rig lane never failed; desktop requests failed at
  once while it was gone (the tunnel listener disappears, so nothing hangs except the request already
  in flight); the tunnel reconnected by itself and the desktop container survived.

### Work recovery: one model at both levels (2026-10-19)

The same shape twice: a GPU (or CPU) worker keeps its own health and pulls tasks from the
orchestration brain; a rig keeps its own health and takes jobs from this controller. Lane logic lives
in llm_orchestration (`shared/agents/`), rig logic here. Build and prove it in the lane, then mirror it.

What each level already had:

| | Orchestration lane (inside a rig) | Controller (across rigs) |
|---|---|---|
| Liveness | process check or GPU heartbeat under 120 s; CPU workers by heartbeat only | rig observe, controller heartbeat (takeover after 120 s) |
| Progress | per-task heartbeat sidecar, class limits (llm 420 s, script 600 s, cpu 900 s, meta 180 s) | none yet (jobs are not plan items) |
| Lost work | orphan: requeue once 120 s stale; stuck: abort, abort, kill, requeue after 60 s; thermal wait exempt | rig reboot: plan held, running item marked interrupted; lost network: rig finishes its queue |
| Retries | 3 attempts, `workers_attempted`, incident history, HUMAN file after | interrupted loads not retried |
| Leases | none on tasks | TTL, renew every ttl/3; expiry never frees a GPU by itself |
| Late results | fixed 2026-10-19: `task_claims.py` (below) | to build |

Combined model (for a task in a lane and a job across rigs):

1. **Classify a missing owner by what is known, not by silence alone.**

   | State | Signal | Action |
   |---|---|---|
   | dead | process gone (in-rig), or rig back with a new boot_id | reassign now; no result is coming |
   | stuck | alive, no progress past the class limit | abort, kill, requeue (lane, as today) |
   | busy-healthy | thermal wait, or progress heartbeat still moving | leave it |
   | unreachable | no heartbeat, no proof of death (network, shared drive) | wait first (rule 2) |

   CPU workers on other hosts and whole rigs are the ones that become `unreachable`; a local GPU
   agent whose process is gone stays on the fast `dead` path.
2. **Wait first for unreachable owners.** Keep giving other work to healthy workers or rigs. Reassign
   the missing owner's work when it is the tail (nothing else left), when it blocks later work
   (critical path), or when it has been gone about 3x its expected runtime (from benchmark history).
   If capacity would otherwise sit idle, duplicate it right away; it only costs power.
3. **First result wins, with a claim id.** Every claim stamps a fresh `claim_id` (scrubbed on requeue).
   A success is accepted if no result exists yet (atomic create); the owner only cleans up its own
   claim; a failure from a claim that moved on is not recorded as a failure (no duplicate retry);
   anything not accepted goes to `late/` for audit. Done in the lane:
   llm_orchestration `shared/agents/task_claims.py` (GPU and CPU workers). Across rigs the same rule
   guards the benchmark ledger: one record per job id.
4. **Progress heartbeats are lease renewals.** An expired task lease means "may be reassigned", not
   "is dead", the same as the GPU lease rule above.
5. **The owner side when cut off:** finish what was claimed, claim nothing new, keep results locally,
   report on reconnect (the rig already finishes its queue; local result buffering is still to build).
6. **Retry memory carries up:** `workers_attempted` becomes `rigs_attempted`; do not hand work back to
   the place that just lost it; three attempts, then a HUMAN file.
7. **Small jobs bound the loss.** Benchmarks split into per-test chunks lose minutes, not campaigns;
   chunks finished before a disconnect are not redone.
8. **Interactive requests fail over at once.** A waiting chat request goes to another rig on the
   first refused connection or a short timeout; rules 2 to 7 are for batch work.

Order of work: (a) claim ids and first-result-wins in the lane - done; (b) this note - done; (c) the
lane's `unreachable` state, wait-first reassignment and the claim as lease - done 2026-10-19, same
rule for GPU and CPU workers (llm_orchestration `workspace/brain-behavior.md`, "Work Recovery", which
also lists every time limit; the only new one is a 4 h LARGE DEFAULT cap). Not yet: expected runtime
from history instead of the cap, duplicating early into idle capacity, `rigs_attempted`. (d) the same
model here when jobs become plan items and there is more than one rig.

Known limits: a cut-off CPU worker's heartbeat thread can still refresh the shared heartbeat file of
its old task after reconnecting (same file name as the new owner's), which can hide a stuck new owner
for a while; a GPU that runs several models, or a big-memory box running several, turns "worker"
into a pool of slots. Both later.

## Same shape as orchestration, one level up

| Orchestration (inside a rig) | Harness (across rigs) |
|---|---|
| brain | controller (Pi manager) |
| GPU workers | rigs |
| `load_llm` / `unload_llm` meta tasks | rig commands (`start remote --detach`, `stop`) |
| worker heartbeats | `rig observe` |
| task queue, workers claim | job queue, rigs claim |
| GPU leases | the rig's lease record |

Risk to design against: double scheduling. Each kind of work needs exactly one layer that decides
placement, or a controller queue and a rig's internal queue will both try to place the same job.

## One model, many slots: capacity vs speed (2026-10-18)

A llama-server holds the weights once and serves `--parallel N` slots; each slot runs one request,
and all active slots advance together in one batched pass. Slot count and total context
(`--ctx-size`) are fixed at load. By default the context is split evenly per slot; with unified KV
(`--kv-unified`) the slots share one pool and each request takes what it needs. Every profile runs
`--parallel 1` today.

Two ways to run five instances of one small model on five 1060s:
- five servers, one per card: five copies of the weights, small fixed context each, cards decode
  independently (aggregate up to about 5x one card);
- one server split across the five cards with five slots: the weights once, the VRAM of the other
  four copies becomes one shared context pool; each step passes through every card over PCIe x1, so
  aggregate speed is somewhere between one card and five, not measured.

Known split costs (`distributed_work_guide.md`): 3090 plus one 1060 cost 2.6-3.1x; Nex on 4-5 1060s
was 6.5x slower than the 3090 alone; 4-way and 5-way decode at the same speed. A small model split
across 1060s vs the same model on one 1060 is not measured.

Planned test (runtime experiment, 1060s):
- one small model; five single-card servers vs one 5-card split server with five slots on unified KV;
- 1, 3 and 5 concurrent requests; record aggregate and per-request tok/s, prompt tok/s, context
  each setup holds (total and largest single request), VRAM per card;
- first check that unified KV shares the pool on the runtime in use (b11669), and that one large
  request can take most of it while others run;
- repeat later for a mid-size model and on the 1070s.

Harness decision (operator direction): capacity vs speed is the harness's call per use, learned
from these measurements over time. Many small or unpredictable tasks, or occasional very large
ones, favour the shared pool; steady small one-shots favour separate servers. Record each result in
the runtime experiments so the harness's placement rules can read them.
