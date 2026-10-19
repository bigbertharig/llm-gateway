# Rig manager: the process everything follows

Status 2026-10-17: Phase A built (leases, claims, placement, observed status, detached rig loads,
gateway cold loads through the manager); lease record moved onto the rig; rig guard built (gates
every load, frees RAM at critical); cache policy per use; Phase C started (lanes lease their GPU).
Next: campaign leases, then B, then D (see "Pick up here").

Design knowledge the manager has to balance (host RAM budget, cache policy per use, layouts, more
than one rig): [DESIGN_NOTES.md](DESIGN_NOTES.md).

## Two layers

| | Pi: controller management (this repo) | Rig: hardware management (`llm_orchestration/shared/scripts/rig/`) |
|---|---|---|
| Decides | who holds which GPUs (leases, claims), placement, ports; later the job queue | nothing about allocation |
| Keeps | nothing persistent (reads the rig) | the lease and claim record, its own mode state, job state |
| Does | sends the rig one command per action, observes, reconciles, reports | runs what it was given on the GPUs it was told; reports `rig observe` |
| Enforces | allocation rules | physical rules, whoever asks: one model load at a time, GPU health, port in use; next: power and thermal guards, VRAM fit |
| Disconnected | reports the rig unreachable | keeps going: finishes running and queued work; new loads wait for new leases |

The Pi never micromanages: it does not sequence loads, poll temperatures or retry around the rig's
rules. Monitoring and safety live on the rig, next to the hardware. The rig is meant to run fully
offline: its state is rig-local, observations and heartbeats exist for the Pi's benefit, and (to
build) results fall back to local storage when the shared drive is gone and sync on reconnect.

## Rules

1. **The rig keeps the record; controllers keep no state.** Leases and claims live on the rig
   (`rig lease ...`, `rig claim ...`, `hw_state.py`, rig-local `shared/brain/hw/gpus/`), written only
   by the rig under the lease store's own lock, in the orchestration schema (`gpu_leases.py`). Every
   write is mirrored to the drive (`/mnt/shared/gpus/`) for orchestration workers when it is mounted.
   A controller reads the record from `rig observe` and asks the rig to change it, so the record
   survives a lost network, and another controller (desktop, laptop) can take over without a
   migration.
1a. **One report, one active controller.** The rig also keeps the active-controller record and the
   plan queue (`hw_plan.py`, rig-local `shared/brain/hw/plan.json`). `rig observe` is the single
   report: "now" (GPUs, profiles, leases, guard) and "next" (the plan). The active controller writes
   the plan and heartbeats; the rig refuses plan, lease and claim writes from any other controller.
   A controller takes control when there is none, when the active one's heartbeat is older than 120 s
   (crashed), or by explicit handoff (`rigctl takeover`); otherwise it is read-only (409). A crashed
   or second controller just reads the report and picks up.
1b. **The rig works through the plan by itself** (`rig-plan.service`): one item at a time, in order,
   then waits. Loaded models stay loaded unless an `unload` item says otherwise. With no controller
   connected it finishes the queue. Item kinds today: `load`, `unload` (jobs such as benchmark lanes
   are the next kind). This mirrors the orchestration task queue in a single file.
2. **No lease, no load.** Every runtime starts under a lease naming its GPUs and port. Every launcher
   already goes through `run_runtime.sh` (brain, workers, split workers, `rig.py`, `run_campaign.py`,
   integration lanes, runtime smoke), so that is where Phase D enforces it.
3. **The port comes from the lease:** 11434 + the lowest GPU index. A lease reserves 11434 + each of
   its GPUs, so two leases never share a port (the worker and benchmark-lane convention already).
4. **Placement is the manager's.** A request gives `gpus_needed` (per card `min_vram_mb`, optional
   `max_vram_mb`) or explicit `gpu_ids`. A card is available only if no lease holds it, nothing runs on
   it, and it is unclaimed (or claimed by the claim the request names). Smallest fit first.
5. **Everything is in the pool by default. A claim takes cards out** (`rigctl claim bench --gpus 0`):
   only requests naming the claim get them. The rig refuses a lease on a claimed card that does not
   name the claim.
6. **Status is observed.** Each GPU gets a verdict from leases plus `rig observe`: `free`, `leased`,
   `loading`, `lease_idle`, `stale` (expired lease), `unleased_busy` (something runs without a lease).
7. **Expiry never frees a GPU by itself.** The manager auto-releases only its own leases with no load
   or runtime behind them; other controllers' expired leases are reported, not touched.

## The client process

```text
1. ask      POST /v1/manager/leases  {controller, owner, run_id, gpus_needed | gpu_ids, claim?, ttl_s, model, purpose}
            -> 201 {lease, port} | 403 wrong owner | 404 unknown claim | 409 held (names holder) | 503 no capacity
2. launch   on the GPUs and port of the lease (Phase C/D: run_runtime.sh --lease <id>)
3. renew    POST /v1/manager/leases/<id>/renew    {owner, ttl_s}, every ttl/3
4. release  POST /v1/manager/leases/<id>/release  {owner}; 409 while a runtime still runs on its GPUs
```

`tools/rigctl.py` wraps all of it (stdlib only; exit codes like `rig`). From the rig:
`ssh pi python3 ~/llm-gateway/tools/rigctl.py ...`. The HTTP API stays on Pi loopback.

## Interactive profiles (Roo)

A cold request for `gpu-rig/<id>`: the manager leases cards for the profile (`interactive`, owner
`llm-gateway`), then queues a `load` item in the rig's plan (`rig plan add`) with the GPUs, port,
lease and cache size it chose. The rig's executor runs it (`rig start remote ... --queue`), waiting its
turn for the rig-wide load lock, and records the profile as `loading` then `ready`; the request is
held until then. A failed item's result comes back as the 502 message and the lease is released. The
reconciler heartbeats, renews leases with a load, runtime or queued item behind them, and releases
orphans. `rigctl unload` queues an `unload` item and releases the lease when it is done.
`rigctl status` shows the controller (this one, the active one) and the last plan items.

## Model loads: any registry model on any cards (2026-10-18)

"Load X on Y cards" is two checks, one per layer:

1. **The harness decides** (`manager/fit.py`, `manager/models.py`). The request names a registry model,
   optionally a card count (smallest free cards first) or explicit GPUs, a context, and a use (benchmark,
   chat, worker) that picks the registry profile and the prompt cache. With no cards named the harness
   chooses: the smallest card class that holds the model on up to two of its cards (one card first), then
   the next class, then the fewest cards of any mix (`fit.choose`). So Ornith-9B goes on one 1060 at
   ctx 26880, or two 1060s when asked for 100k; a 27B goes on the 3090, not three 1060s. The manager reads the model's GGUF header
   from the drive (no weights) and builds what llama.cpp will allocate per card: layer weights (the token
   embedding stays in host RAM, the output layer goes on the last card, next-token-prediction layers cost
   nothing), q8_0 KV per attention layer (hybrid and sliding-window layers counted as such), recurrent
   state, the compute buffer (grows with context x micro-batch x heads) and a 512 MiB margin per card.
   It plans the largest context that fits (capped at the trained context or the request, floor 4096),
   balances a contiguous layer split across the cards, and gives exact llama.cpp arguments:
   `--ctx-size`, `--tensor-split` as layer counts, `--n-gpu-layers all`. If it will not fit it answers
   422 "won't fit" with the numbers and the free cards that would hold it; nothing is leased or sent.
2. **The rig loads and confirms** (`rig start remote <id> --spec JSON`, via a plan `load` item). It runs
   the spec through `run_runtime.sh` on the leased cards, then reads llama.cpp's own startup log
   (`scripts/rig/hw_loadlog.py`): the fit projection per card, layers offloaded, the context allocated
   and the buffers per card. A load that is not fully on the GPUs is refused and unloaded. A ready load
   records `fit` (measured per card) and the real `ctx_size` in its rig profile, and the manager returns
   both next to its estimate.

Layers are never left to llama.cpp's `--fit`: given the choice it moves layers to the CPU (on
Ornith-9B it balanced two 1060s by putting the output layer in host RAM, 33/34 layers on GPU). The
estimate was checked on Ornith-1.5-9B over two 1060s: weights 4812 MiB and KV 17.0 KiB per token,
the same as llama.cpp measured.

```text
rigctl fit ornith-1.5:9b [--ctx 100000]         # answer only; the harness picks the cards
rigctl fit ornith-1.5:9b --cards 2              # answer only: ctx, split, MiB per card, or won't fit
rigctl load-model ornith-1.5:9b --gpus 1,3 [--ctx N] [--use benchmark] [--args "--reasoning on"]
rigctl unload ornith-1.5-9b-g1-3                # profile id: model plus its cards
```

HTTP: `POST /v1/manager/models/fit`, `POST /v1/manager/models/load` with
{model, cards | gpu_ids, ctx?, ctx_min?, use?, profile?, claim?, margin_mb?, extra_args?}.
The lease is the manager's (owner llm-gateway; controller by use: benchmarks, interactive, plans), so
the reconciler renews it while the rig profile lives and `rigctl unload` releases it.

Open: calibrating the estimate from the measured `fit` records (the compute buffer is the loosest
term, within about 10% on the 1060s); a VRAM check for published profiles; context per slot with
`--parallel`.

## Requirements gathered (2026-10-17 benchmark lanes)

- **Job queue:** benchmark lanes become jobs the manager schedules: each job states its model and
  card needs, and the next one that fits starts when a card frees up. Adding or removing cards
  mid-run is a claim change; stopping a run cancels its jobs (one stop path for every job).
- **VRAM fit before placement:** a per-model VRAM estimate (weights + KV at the profile's ctx) from
  the registry, checked against the card (ministral-3:8b at 16k ran at 5.97 of 6.1 GB). Built
  2026-10-18 for model loads ("Model loads" above); published profiles still use fixed shapes.
- **One rig guard:** a single always-on rig-side power and thermal guard that every job runs under
  (today `startup_thermal_monitor.py` is started per run, and a lane started by hand sat outside it).
- **Runtime memory policy per use** (run notes `logs/benchmarks/worker_lanes_20261017_203028/RUN_NOTES.md`
  section 9): host prompt cache (`--cache-ram`, llama.cpp default 8192 MiB per server), ctx, slots,
  KV type and placement, mmap, container memory limits. Registry settings per model, merged at load
  time with a use profile (benchmark: cache-ram 0, parallel 1; agent: small cache; chat: bounded cache
  on the 3090 profile), plus a rig-wide RAM budget next to the VRAM fit check. Evidence: five lanes x
  8 GB default cache against 31 GB RAM, earlyoom killed a lane's runtime at 21:41.

## Migration

| Phase | Scope | Behavior change |
|---|---|---|
| A (built) | Manager: leases, claims, placement, status, reconcile, `rigctl`; rig: `observe`, detached placed loads | Roo path only; everything else is reported (`unleased_busy`), not blocked |
| Rig guards | Always-on power/thermal guard and VRAM fit on the rig; one stop path for rig jobs; local results fallback | Safety independent of the Pi |
| B | Orchestration stays a mode for now (likely folded into the manager later); its brain and workers take leases through `rigctl` | Plans visible and protected |
| C (after the current benches) | Benchmark lanes and campaigns become manager jobs / lease holders | Benchmarks visible and protected |
| D | `run_runtime.sh` refuses a load without a valid lease | Nothing loads outside the process |

## Pick up here (entry points per phase)

**B, orchestration (left as a mode for now; likely folded into the manager later).**
- `rig start orchestration|bench` (`rig.py` `start_stack`) become manager requests: the manager leases
  the brain's GPU and the worker GPUs (`plans` controller) before starting the stack, releases on stop.
- Workers already refuse plan claims on leased GPUs (`agents/gpu.py` `_read_gpu_lease`, reading
  `/mnt/shared/gpus/leases.json`, now the rig's mirror). Change the reader to the rig-local record
  (`shared/brain/hw/gpus/`, `scripts/rig/hw_state.py`) so it works without the drive.
- Brain meta tasks (`agents/brain_resources.py` `_insert_resource_task`, executed by
  `agents/gpu_llama.py` `load_model`) keep their shape; a load_llm targets only GPUs under the
  stack's lease.
- Open: does the brain's model-choice logic move into the manager (the user expects so, not decided).

**C, benchmarking.** Started 2026-10-17: lanes (`worker_lane.sh`) hold a `benchmarks` lease per GPU
through `harness_lease.sh` (rigctl over ssh), pass it to `run_runtime.sh`, release it on exit; a
refused card stops the lane (`LANE_REFUSED`), an unreachable manager runs offline as `no-lease`.
`LANE_STOP_AFTER=3` gives a quick check. Still open below: campaigns, the job queue, run-note fixes.
- Lane scripts: `benchmarking/scripts/integration/run_worker_lanes.sh`, `worker_lane.sh`, `phase4.sh`,
  `p45.sh`; campaigns: `llm_orchestration/shared/scripts/benchmarks/run_campaign.py`.
- Each lane takes a `benchmarks` lease for its GPU (`rigctl acquire --gpus N`, renew in its loop,
  release at LANE_DONE); campaigns the same per lane. Drop `BENCHMARK_DISABLE_AUTO_RESERVE`.
- Then the job queue: a lane run becomes a set of manager jobs (model + card needs) instead of
  fixed lanes, so cards can join or leave mid-run and one stop cancels everything.
- Run-note fixes to fold in: `RUN_NOTES.md` sections 1-5 in `logs/benchmarks/worker_lanes_20261017_203028/`.

**D, enforcement.**
- `run_runtime.sh` gains `--lease <id>` and checks it against the rig record (`rig lease list`): the
  GPUs and port it is asked to start must be held by that lease. Every launcher in rule 2 passes it.
- Audit first (log loads without a lease for a while), then refuse.

**Rig guards (in progress 2026-10-17):** always-on `rig-guard.service` on the rig, see `hw_guard.py`.
