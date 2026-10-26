# Rig manager: the process everything follows

Status 2026-10-24: Phase A built (leases, claims, placement, observed status, detached rig loads,
gateway cold loads through the manager); lease record moved onto the rig; rig guard built (gates
every load, frees RAM at critical); cache policy per use; Phase C built (lanes and campaigns load through
the manager); isolated rig modes (one mode at a time: a benchmark load runs in `bench`, chat and worker
loads in `remote`). Next: B, then D (see "Pick up here").

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
1c. **One report, two copies (2026-10-18).** The rig's record and plan are also kept by the active
   controller: every observation (the 30 s heartbeat) saves `rig observe` to the controller's own disk
   (`RIG_MIRROR_PATH`, default `~/.local/state/llm-gateway/rig_report.json`). The plan has a `version`
   (bumped on every rig write) and the rig's `boot_id`. Either side recovers from the other:
   - rig reboots: on the first start of a new boot it marks the item that was running `interrupted`
     (with what it was doing) and holds the plan; it boots neutral and runs nothing. The controller's
     reconciler sees the hold, writes its copy back if the rig's version is behind (lost or replaced
     file, `rig plan restore`), reports what was interrupted and resumes (`rig plan resume`).
     Interrupted loads are not retried (their caller got the failure); their leases are released as
     unbacked. A lost network without a reboot is not a hold: the rig finishes what it was given.
   - rig unreachable: status answers from the copy (`from_copy`, "RIG UNREACHABLE since ...") and the
     controller logs when the rig went away, when it came back and whether it rebooted (`rig.events`).
   - controller crashes: it restarts with its copy and reads the rig's live report.
   Runs (2026-10-22): long work (a campaign, a lane) started with `rigctl run-start` is a `job` plan item
   and a record in the plan's `runs`, so it is in the report and both copies. After a reboot a run that was
   running is listed as interrupted in the hold, and the controller asks instead of acting: "resume from
   checkpoint?" (`rigctl recover RUN`, the dashboard's Resume) or dismiss. A run that opted in
   (`--auto-resume`) is resumed on its own. A lost network is not an interruption: the run's unit keeps
   going, and the manager's waits keep waiting (see "Runs" below). Work started outside `run-start` (a bare
   `systemd-run`) is still invisible to the report.
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

1. **The manager decides** (`manager/fit.py`, `manager/models.py`). The request names a catalog model,
   optionally a card count (smallest free cards first) or explicit GPUs, a context, and a use (benchmark,
   chat, worker) that picks the registry profile and the prompt cache. With no cards named the harness
   chooses by use: chat first tries the largest single free card (the 3090 on this rig) for lower
   latency, then falls back to the usual split fit. Benchmark and worker loads compare measured response
   speed across fitting, available card sets when
   two comparable observations exist; otherwise they use the smallest card class that holds the model
   on up to two cards, then the next class, then the fewest cards of any mix (`fit.choose`). Explicit GPU
   requests always keep their named cards.
   For example, a benchmark Ornith-9B goes on one 1060 at ctx 26880, or two 1060s when asked
   for 100k; a 27B goes on the 3090 instead of three 1060s. Chat Ornith-9B uses the 3090 when free. The manager reads the model's GGUF header
   from the drive (no weights) and builds what llama.cpp will allocate per card: layer weights (the token
   embedding stays in host RAM, the output layer goes on the last card, next-token-prediction layers cost
   nothing), q8_0 KV per attention layer (hybrid and sliding-window layers counted as such), recurrent
   state, the compute buffer (from this GGUF's measured loads, below) and a margin per card.
   An unspecified context uses the selected profile's working context for every use. A request can
   instead set `context_mode=min` to use the profile's minimum or `context_mode=max` to find the
   largest fit up to its maximum. With no cards named, `min` uses the fewest cards that fit and
   `max` uses all free cards; with a card count, the manager searches available card classes of
   that size. An explicit `ctx` is exact and is checked against the profile/trained maximum. The manager
   balances a contiguous layer split across the cards, and gives exact llama.cpp arguments:
   `--ctx-size`, `--tensor-split` as layer counts, `--n-gpu-layers all`. If it will not fit it answers
   422 "won't fit" with the numbers and the free cards that would hold it; if the same named cards
   fit at the profile minimum, it suggests `context_mode=min`. Nothing is leased or sent.
2. **The rig loads and confirms** (`rig start remote <id> --spec JSON`, via a plan `load` item). It runs
   the spec through `run_runtime.sh` on the leased cards, then reads llama.cpp's own startup log
   (`scripts/rig/hw_loadlog.py`): the fit projection per card, layers offloaded, the context allocated
   and the buffers per card. A load that is not fully on the GPUs is refused and unloaded. The spec's use
   picks the rig mode: `benchmark` runs in `bench`, anything else in `remote`, and the rig refuses a load
   for another mode while one is active (isolated modes, 2026-10-23, `rig.py` `load_mode`). A ready load
   records `fit` (measured per card) and the real `ctx_size` in its rig profile, and the manager returns
   both next to its estimate.

**Model requirements versus placement (2026-10-25).** The catalog needs the
GGUF identity and runtime settings (for example micro-batch, KV type,
parallelism, reasoning flags, and any supported context limits). It does
not need a GPU list or a saved layer split for each model. Context is an
input to the fit: full-attention KV use grows roughly per token, sliding
windows stop growing past their window, recurrent state can be fixed, and
weights stay fixed for a chosen partition. Compute buffers, graph
allocation, per-card overhead, and margins are calibrated from load
observations because they are not one perfectly linear context formula.
The manager computes the card set and `tensor_split` for the requested
context and available memory, then validates the actual allocation. Compute
is calibrated per card count. With two comparable speed observations for the
same GGUF, runtime image, micro-batch, and nearby context, automatic placement
chooses the card set with the shortest estimated 1,024-token prompt plus
256-token response. Other models retain the existing `SAME_CLASS_MAX_CARDS=2`
placement fallback. Calls without `ctx` use the selected profile's working context by default.
The library records `context_bounds.min` and `context_bounds.max` for current models; the maximum
is the GGUF trained limit, not a guarantee of quality or a tested fit. `min` is a deliberate
lower-context request, and `max` is an opt-in capacity search. The manager still checks
measured OOM ceilings, card memory, and real allocation at load. An explicit `ctx` remains exact.
The imported `schedule.placement` labels feed a legacy
orchestration/dashboard path; manager fit does not use them. For example
`ministral-3:8b` is labeled `single_gpu` there. Its default 16K fit on one
1060 was refused, while `context_mode=min` now plans 8K on that card; `max` can project
the largest fit on a chosen two-card split.

### Context capacity and GPU pools (decision recorded 2026-10-26)

The library's `runtime.ctx_size` is a **working context** for a use or benchmark.
`context_bounds.min` is currently only a **planning floor** (4K or 8K),
not the model's compact baseline. `context_bounds.max` is the GGUF trained
ceiling, not a measured promise that the model fits or recalls correctly at
that depth. Keep these three facts separate in results and API responses.

The desired compact baseline is **the largest measured context that fits on
the smallest preferred, homogeneous GPU group**. A worker-pool model starts
with the fewest GTX 1060s that can hold it; a brain-pool model starts with
the single RTX 3090 Ti when it fits. Add cards within the same pool to reach
longer contexts. At the trained ceiling, record the fewest cards that
actually load and pass depth/recall checks. If no pure pool can hold the
requested context, a 3090+1060 split is the last capacity option. If all
six cards still fall short, record the highest validated context and mark
the trained ceiling unreachable on this rig.

A brain-pool model may have two placements for the **same context, batch,
GGUF and runtime options**: fast on the 3090, slower on 1060s only. When
the 3090 is busy, the slower homogeneous placement can be used if it fits;
it must not silently shrink context. A separately requested lower-context
run is a different capacity choice. Worker-pool and brain-pool are
placement preferences, not model identities or saved layer splits.

**Implementation status:** `context_mode=min` currently means the 4K/8K
planning floor, and `context_mode=max` currently estimates the largest
fit on the selected cards (all free cards if none are named). The automatic
planner does not yet enforce the pool ordering or provide the fast/slow
fallback described above. The benchmarking
`CONTEXT_PROFILE_AUDIT_2026-10-26.md` is a read-only capacity projection;
it is not the measured compact-baseline/full-ceiling matrix. Keep working
contexts stable for benchmark comparability while the placement policy and
capacity tests are added.

Layers are never left to llama.cpp's `--fit`: given the choice it moves layers to the CPU (on
Ornith-9B it balanced two 1060s by putting the output layer in host RAM, 33/34 layers on GPU). The
estimate was checked on Ornith-1.5-9B over two 1060s: weights 4812 MiB and KV 17.0 KiB per token,
the same as llama.cpp measured.

```text
rigctl fit ornith-1.5:9b [--ctx 100000]         # answer only; the harness picks the cards
rigctl fit ornith-1.5:9b --cards 2              # working context on a fitting two-card set
rigctl fit ministral-3:8b --gpus 1 --context-mode min   # 8K on one 1060
rigctl fit ministral-3:8b --gpus 1,2 --context-mode max # largest projected fit on that split
rigctl load-model ornith-1.5:9b --gpus 1,3 [--ctx N] [--use benchmark] [--args "--reasoning on"]
rigctl unload ornith-1.5-9b-g1-3                # profile id: model plus its cards
```

HTTP: `POST /v1/manager/models/fit`, `POST /v1/manager/models/load` with
{model, cards | gpu_ids, ctx?, context_mode? (working|min|max), ctx_min?, use?, profile?,
claim?, margin_mb?, extra_args?}. `ctx` and a non-working `context_mode` are mutually exclusive.
The lease is the manager's (owner llm-gateway; controller by use: benchmarks, interactive, plans), so
the reconciler renews it while the rig profile lives and `rigctl unload` releases it.

Card size has two meanings. Placement plans on empty cards (total memory: what a set of cards can hold
once its current work frees, as the collecting wait uses it); a fit for an immediate load uses free memory
(total minus what is loaded now). Later: use the active size to stack several small jobs on one card.

**Models come from the gateway's own catalog (2026-10-25).** The manager never reads benchmarking at
runtime. `tools/generate_profiles.py` imports the benchmarking model library into `model_catalog.json`
(every model, its resolved profiles, the roles, the runtime image, the dashboard's model summary) and
`remote_profiles.json` (the offer); `manager/models.py` resolves from the catalog. Re-run the importer to take
in what benchmarking has learned. A published profile names no cards: `load_profile` plans it like any model
load (the largest context that fits, at least the profile's `ctx_size`) under the profile's own id. When the
free cards cannot hold it but an idle rig could, the answer is 503 (busy, retry), not 422 (won't fit).

**Learning from loads (2026-10-25).** The manager keeps its own history in this repo,
`lessons/load_observations.jsonl` (`manager/observations.py`; `OBSERVATIONS_PATH` overrides), committed, so
it carries across sessions and machines; it is also the gateway's lessons file for benchmarking to read
(`lessons/README.md`). A `load` row has the model, GGUF, micro-batch, use, the cards by UUID with link and
root port (the rig's `gpu_scan`), the estimate per card, what llama.cpp allocated per card (the rig's `fit`),
memory in use per card once serving, and the load time; a `load_failed` row has the same facts, the error and
whether it ran out of memory. Started 2026-10-25 from the loads in the benchmarking capacity records (marked
`from_record`). The fit plans from it, each load counted once (`observations.history`):

- compute buffer per card (`fit.compute_mb`): measured loads for the same GGUF, micro-batch, and
  card count, padded by 10% and 64 MiB. A measured buffer is never discarded when it exceeds the
  formula. Unmeasured combinations use the formula until a matching load is recorded.
- overhead per card (`fit.overhead_mb`): the highest observed serving memory beyond llama.cpp's
  reported buffers for a card of that size, plus 64 MiB; 150 MiB until measured.
- growth margin (`fit.growth_margin_mb`): four times observed full-depth memory growth, with a
  128 MiB minimum, only for the same GGUF and card-size layout. Other layouts retain 512 MiB.
  The qwen3.6:35b-a3b full-depth probes grew 18 MiB on the 3090 and 8 MiB per 1060, giving
  both measured layouts a 128 MiB margin.
- an OOM ceiling (`fit.failed_ceiling`): a prior OOM blocks explicit and automatically chosen
  contexts at or above the failure for that GGUF and card-size layout until a higher load succeeds.
- measured placement (`fit._measured_placement`): compares observed prefill and decode for fitting,
  free card sets with the same GGUF, micro-batch, runtime image, and nearby context. For
  `ministral-3:8b` at 16K, GPU0 measured 2,616/121.8 tok/s prefill/decode versus 475/20.2 on
  two 1060s; the manager selects GPU0 when both choices are free. Explicit GPU requests prevail.

The plan reports `compute_basis`, `overhead_mb`, `margin_mb`, `failed_ceiling` where present, and
`performance_basis` when measured placement applies. `rigctl observations [--kind KIND]
[--model M]` lists the history. The rig still checks llama.cpp's own projection. The corrected
planner admitted qwen3.6:35b-a3b at 230,144 on GPU0, and a real managed load reached ready in
84.5 seconds and decoded at 148.34 tok/s. The known qwen3-coder:30b-a3b 120K OOM boundary
remains blocked.

**Prefill tuning (2026-10-25).** Under the standing power caps, matched 131K qwen3.6:35b-a3b runs on the 3090 measured 1.87-2.32x prefill from batch 128 to 512 with unchanged depth decode and 138 MiB more compute buffer. The current qwen3.6:27b brain measured 1.41-1.46x prefill from its orchestration batch 64 to 512 at 16K, with unchanged depth decode and 101 MiB more compute. All recall probes passed. Benchmarking also measured a smaller 1.2-1.3x gain for gemma-4:31b at 32K. The imported catalog now gives these three models batch 512 in the relevant interactive/orchestration profiles; the benchmark defaults remain unchanged. The manager resolves the catalog by use at load time. See benchmarking `RIG_PERFORMANCE.md`, "Micro-batch at working context"; load time is cold-cache dependent and is not improved by this setting.

Open: a VRAM check for published profiles; context per slot with `--parallel`.

## Runs, cancel by owner, and demand (2026-10-22)

**Runs.** `rigctl run-start --run ID [--resume "CMD"] [--auto-resume] [--needs MIB[xN]] -- CMD...` hands the rig
a long run: the rig's plan executor starts it as its own unit (`rig-run-ID`, as bryan) and records it in the
plan's `runs` (owner, command, resume command, needs, attempts). Its state follows the unit: running, then
finished (exit 0) or failed; `stopped` and `dismissed` are set by the controller. The reconciler reports each
run that ends, once. `rigctl runs` lists them; `rigctl recover RUN` starts an interrupted, failed or stopped run
again with its resume command (for a campaign, the same command with `--run-id`, which continues from
`campaign_state.json`); `rigctl run-stop RUN` stops it and cleans up; `rigctl dismiss RUN` marks an ended run as
not to be resumed and cleans up. The dashboard's rig tab lists runs with Resume, Stop and Dismiss.

**Cancel by owner.** `rigctl cancel-owner WHO` (`POST /v1/manager/owners/WHO/cancel`) cleans up everything one
owner has, with no time limits: stops its running runs, cancels its queued plan items, unloads its models,
releases its leases and drops its claims (which also cancels its waits), and logs what it did. It matches
leases by owner, run id or `requested_by` (a load's `--owner`), claims by owner or name (and `campaign-WHO`,
the campaign runner's reservations), runs by run id or owner, and waits by owner. Stopping or dismissing a run
calls it for the run's owner.

**Demand.** The manager keeps its waits in progress (collecting and named-card waits, with the card sizes
each needs) and reads the `needs` of running runs. A load that names no cards takes the smallest cards that
hold it; when those are busy and it would spill onto a bigger card (a 1060 model onto the 3090), it does so
only if no other work is waiting for that size. Otherwise it uses other free cards, or answers 409 naming what
it kept free and for whom. A requester's own waits and runs do not count against it. Demand across callers
stops there: the rig runs one mode at a time, so nothing in another mode competes for the cards.

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

## Reservations: split loads wait for their cards (2026-10-20)

One rule for every split load, whoever starts it (orchestration's brain, a benchmark lane, a campaign,
the gateway): a split that cannot start because some of its cards are busy **reserves all of them**
and waits. Single work can never take a reserved card, so the split gets a matching set as soon as the
current holders finish. Without this, single tasks take every card as it frees and splits starve.

The primitive is the rig's claim (`hw_state.py`), extended:

| Field | Meaning |
|---|---|
| `name`, `gpu_ids` | as before |
| `owner` | who renews it (a lane, a campaign run, a manager wait, orchestration) |
| `ttl_s`, `expires_at` | optional. A claim with a TTL lapses unless its owner renews it, so a dead run frees its cards; a claim without one is permanent until removed (the old behavior) |
| `purpose`, `model` | what it waits for, for status and for an operator deciding to override |

Rules (automatic):

1. A claim can be placed on busy cards. The current holder keeps them until it releases; from then on
   the rig refuses them to any lease that does not name the claim (rule 5 above).
2. Expired claims are ignored for placement and dropped from the record on the next claim write (each
   drop is logged in `gpus/claims_expired.log`).
3. A split load with `wait_s` (`rigctl load-model --gpus 1,3 --wait 7200`): when its cards are not all
   free, the manager reserves them (or uses the claim the request names), renews the reservation while
   it waits, and loads under it once every card is free. Past `wait_s` it drops the reservation and
   answers 504. A reservation the manager made for the wait is dropped once the load's lease holds the
   cards.
4. A lane claims its card group with a TTL and renews it while it runs, and loads with `--wait`.
4a. A load that names no cards, with `wait_s` (`rigctl load-model MODEL --wait forever`), collects its cards
   (2026-10-22): the plan the harness would make on an idle rig says how many cards of which size the model
   needs (for mellum2.1, two 1060s), and the manager claims free cards of those sizes one at a time as they
   free, in one claim it extends (`rig claim extend`), so single work cannot keep taking each card as it
   frees. It loads once the claim holds the set, and never takes a card of another size (a 1060 model does
   not collect the 3090). `wait_s` -1 (`--wait forever`) waits with no time limit, for collecting and for
   named-card waits alike. Removing the wait's claim (`rigctl unclaim wait-...`) cancels it: the load
   answers 409 "cancelled" and nothing is left behind. A wait whose claim lapses (the manager stopped
   renewing it) cancels the same way instead of re-taking it. While the rig is unreachable every wait keeps
   waiting (the rig keeps working; a lost network is not a crash), and a claim that lapsed during the outage
   (nobody could renew it) is taken again when the rig is back, not read as a cancel.
5. A campaign's runtime group loads like any other model load (`rigctl load-model --use benchmark --owner
   campaign-<run> --wait <s>`): pinned cards wait as in rule 3, a group that names no cards collects them as
   in 4a. Past its wait limit (2 h by default) the block fails with the manager's answer.
6. Orchestration's split reservations (`agents/gpu_split.py`: `waiting_partner` -> `joining` ->
   `loading` -> `ready`) also hold a rig claim (`plans-split-<group>`, owner `plans`) for as long as the
   reservation is in a loading state, renewed by the launcher while it waits; and orchestration workers
   skip work on a card held by a claim that is not theirs, as they already do for leases.

Operator overrides (`rigctl`, act immediately, logged in the manager's events):

| Command | Does |
|---|---|
| `rigctl claim NAME --gpus 1,3 [--ttl S]` / `rigctl unclaim NAME` | reserve or free cards by hand (unclaim works on anyone's claim) |
| `rigctl claims` | list claims with owner, expiry and purpose |
| `rigctl kill --gpus 1,3` | stop every runtime on those cards, release their leases, drop claims on them |
| `rigctl load-model MODEL --gpus 1,3 --force` | kill whatever is on those cards, then load |
| `rigctl unload PROFILE` | unload a profile (releases its lease) |
| `scripts/assign_task.py TASK_ID gpu-N` (llm_orchestration) | pin a queued task to one worker (`target_worker`, honored for work tasks as well as meta tasks) |

## Migration

| Phase | Scope | Behavior change |
|---|---|---|
| A (built) | Manager: leases, claims, placement, status, reconcile, `rigctl`; rig: `observe`, detached placed loads | Roo path only; everything else is reported (`unleased_busy`), not blocked |
| Rig guards | Always-on power/thermal guard and VRAM fit on the rig; one stop path for rig jobs; local results fallback | Safety independent of the Pi |
| B | Orchestration stays a mode for now (likely folded into the manager later); its brain and workers take leases through `rigctl` | Plans visible and protected |
| C (built) | Benchmark lanes and campaigns load through the manager and run as manager runs | Benchmarks visible and protected |
| D | `run_runtime.sh` refuses a load without a valid lease | Nothing loads outside the process |

## Pick up here (entry points per phase)

**B, orchestration (left as a mode for now; likely folded into the manager later).**
- `rig start orchestration` (`rig.py` `start_stack`) becomes a manager request: the manager leases
  the brain's GPU and the worker GPUs (`plans` controller) before starting the stack, releases on stop.
- Workers already refuse plan claims on leased GPUs (`agents/gpu.py` `_read_gpu_lease`, reading
  `/mnt/shared/gpus/leases.json`, now the rig's mirror). Change the reader to the rig-local record
  (`shared/brain/hw/gpus/`, `scripts/rig/hw_state.py`) so it works without the drive.
- Brain meta tasks (`agents/brain_resources.py` `_insert_resource_task`, executed by
  `agents/gpu_llama.py` `load_model`) keep their shape; a load_llm targets only GPUs under the
  stack's lease.
- Open: does the brain's model-choice logic move into the manager (the user expects so, not decided).

**C, benchmarking (built).** Lanes (`benchmarking/scripts/integration/worker_lane.sh`) claim their card
group and load with `rigctl load-model --use benchmark`; campaigns (`llm_orchestration/shared/scripts/
benchmarks/run_campaign.py`) load each runtime group the same way with `--owner campaign-<run>` and cancel
that owner when they end. Both start as runs (`rigctl run-start`). Open: drop
`BENCHMARK_DISABLE_AUTO_RESERVE`; the run-note fixes in `RUN_NOTES.md` sections 1-5
(`logs/benchmarks/worker_lanes_20261017_203028/`).

**D, enforcement.**
- `run_runtime.sh` gains `--lease <id>` and checks it against the rig record (`rig lease list`): the
  GPUs and port it is asked to start must be held by that lease. Every launcher in rule 2 passes it.
- Audit first (log loads without a lease for a while), then refuse.

**Rig guards (in progress 2026-10-17):** always-on `rig-guard.service` on the rig, see `hw_guard.py`.
