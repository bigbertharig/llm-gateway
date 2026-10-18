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
