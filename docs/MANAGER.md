# Rig manager: the process everything follows

Status 2026-10-17: Phase A built (lease API, claims, placement, observed status, detached rig loads,
gateway cold loads through the manager). Phases B-D and the rig guards are next (see Migration).

## Two layers

| | Pi: controller management (this repo) | Rig: hardware management (`llm_orchestration/shared/scripts/rig/`) |
|---|---|---|
| Decides | who holds which GPUs (leases, claims), placement, ports; later the job queue | nothing about allocation |
| Does | sends the rig one command per action, observes, reconciles, reports | runs what it was given on the GPUs it was told; reports `rig observe` |
| Enforces | allocation rules | physical rules, whoever asks: one model load at a time, GPU health, port in use; next: power and thermal guards, VRAM fit |
| Disconnected | reports the rig unreachable | keeps going: finishes running and queued work; new loads wait for new leases |

The Pi never micromanages: it does not sequence loads, poll temperatures or retry around the rig's
rules. Monitoring and safety live on the rig, next to the hardware. The rig is meant to run fully
offline: its state is rig-local, observations and heartbeats exist for the Pi's benefit, and (to
build) results fall back to local storage when the shared drive is gone and sync on reconnect.

## Rules

1. **One writer.** Only the manager grants, renews and releases GPU leases. The store keeps the
   orchestration contract and file (`shared/agents/gpu_leases.py`, `shared/gpus/leases.json` on the
   drive, the rig's `/mnt/shared/gpus/leases.json`), so GPU workers and the brain read it unchanged.
2. **No lease, no load.** Every runtime starts under a lease naming its GPUs and port. Every launcher
   already goes through `run_runtime.sh` (brain, workers, split workers, `rig.py`, `run_campaign.py`,
   integration lanes, runtime smoke), so that is where Phase D enforces it.
3. **The port comes from the lease:** 11434 + the lowest GPU index. A lease reserves 11434 + each of
   its GPUs, so two leases never share a port (the worker and benchmark-lane convention already).
4. **Placement is the manager's.** A request gives `gpus_needed` (per card `min_vram_mb`, optional
   `max_vram_mb`) or explicit `gpu_ids`. A card is available only if no lease holds it, nothing runs on
   it, and it is unclaimed (or claimed by the claim the request names). Smallest fit first.
5. **Everything is in the pool by default. A claim takes cards out** (`rigctl claim bench --gpus 0`):
   only requests naming the claim get them. Claims live in `shared/gpus/claims.json`.
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
`llm-gateway`), then sends one command, `rig start remote <id> --gpus .. --port .. --lease .. --detach`.
The rig runs the load as its own systemd job (`rig-load-<id>`, `hw_jobs.py`), waits its turn for the
rig-wide load lock, and records the profile as `loading` then `ready`; the request is held until
then. A failed job's log tail comes back as the 502 message and the lease is released. The
reconciler renews leases with a load or runtime behind them and releases orphans. `rigctl unload`
stops the profile and releases its lease.

## Requirements gathered (2026-10-17 benchmark lanes)

- **Job queue:** benchmark lanes become jobs the manager schedules: each job states its model and
  card needs, and the next one that fits starts when a card frees up. Adding or removing cards
  mid-run is a claim change; stopping a run cancels its jobs (one stop path for every job).
- **VRAM fit before placement:** a per-model VRAM estimate (weights + KV at the profile's ctx) from
  the registry, checked against the card (ministral-3:8b at 16k ran at 5.97 of 6.1 GB).
- **One rig guard:** a single always-on rig-side power and thermal guard that every job runs under
  (today `startup_thermal_monitor.py` is started per run, and a lane started by hand sat outside it).

## Migration

| Phase | Scope | Behavior change |
|---|---|---|
| A (built) | Manager: leases, claims, placement, status, reconcile, `rigctl`; rig: `observe`, detached placed loads | Roo path only; everything else is reported (`unleased_busy`), not blocked |
| Rig guards | Always-on power/thermal guard and VRAM fit on the rig; one stop path for rig jobs; local results fallback | Safety independent of the Pi |
| B | Orchestration stays a mode for now (likely folded into the manager later); its brain and workers take leases through `rigctl` | Plans visible and protected |
| C (after the current benches) | Benchmark lanes and campaigns become manager jobs / lease holders | Benchmarks visible and protected |
| D | `run_runtime.sh` refuses a load without a valid lease | Nothing loads outside the process |
