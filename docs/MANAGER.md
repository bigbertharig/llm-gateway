# Rig manager: the process everything follows

Status: proposed 2026-10-17. Phase A is the next build; B-D follow (see Migration).

The harness (this repo, on the Pi) is the one manager for the rig's GPUs. Roo, the orchestration
brain and workers, and benchmark campaigns are all clients. Each one asks the manager for GPUs
before loading a model, and the manager is the only thing that decides who holds which card.

## Why

On 2026-10-17 four controllers placed models with no shared view: `rig.py` remote mode (own
allocator, looks only at free VRAM), the orchestration brain (lease store), benchmark launchers
(`run_runtime.sh` directly, `BENCHMARK_DISABLE_AUTO_RESERVE=1`) and the gateway (through `rig.py`).
Results: `rig status` said idle while a 3090 campaign and four 1060 benchmark lanes ran; a 1060
profile was placed on the 3090; remote profiles took ports 11434-11441 while benchmark lanes used
11435-11438. The only shared guards were `run_runtime.sh`'s load lock and occupied-port check.

## Rules

1. **One writer.** Only the manager grants, renews and releases GPU leases. Everyone else reads.
   The store keeps the existing contract and location (`llm_orchestration/shared/agents/gpu_leases.py`
   schema, `shared/gpus/leases.json`), so workers and the brain keep reading it unchanged. A single
   writer also removes cross-host file locking (the Pi writes the drive locally, the rig over NFS).
2. **No lease, no load.** Every llama runtime starts under a lease that names its GPUs and port.
   Every launcher already goes through `run_runtime.sh` (brain, workers, split workers, `rig.py`,
   `run_campaign.py`, integration lanes, runtime smoke), so that is where it is enforced.
3. **The port comes from the lease:** 11434 + the lowest GPU index in it (GPU 0 = 11434,
   GPU 1 = 11435, ...). That is the existing worker and benchmark-lane convention, and since a lease
   is exclusive on its GPUs, two leases can never get the same port.
4. **Placement is the manager's.** A client states what it needs (`gpus_needed`: per card
   `min_vram_mb` and optional `max_vram_mb`, or explicit GPU ids for benchmarks that must pin a card).
   The manager picks the cards, smallest fit first, honoring admission.
5. **Admission is operator intent.** Per controller, which GPUs it may lease (for example "3090:
   benchmarks only today"). It extends `control_policy.json` and the manager owns it.
6. **Status is observed, not assumed.** The manager reconciles leases against what the rig shows
   (`nvidia-smi`, llama containers, port identities) and reports differences instead of hiding them.
7. **Expiry never frees a GPU by itself** (existing lease rule). The manager releases an expired
   lease only after checking that its runtime is gone; if it is still running, it is reported stale.

## The client process

```text
1. ask      POST /v1/manager/leases        {controller, owner, run_id, gpus_needed | gpu_ids,
                                             ttl_s, model, purpose}
            -> 201 {lease_id, gpu_ids, port, expires_at}
            -> 403 not admitted | 409 held (names the holder) | 503 no capacity (says what is free)
2. launch   run_runtime.sh --lease <lease_id> ...   (refuses GPUs or a port the lease does not hold)
            or, for interactive profiles, the manager launches it (`rig start remote` with the lease)
3. renew    POST /v1/manager/leases/<id>/renew      every ttl/3 while the runtime is in use
4. release  DELETE /v1/manager/leases/<id>          after stopping the runtime; 409 if it still serves
```

Controllers are the existing ones: `plans`, `benchmarks`, `interactive`, `supervision`, `system`.
Clients on the rig (brain, workers, benchmark launchers) reach the manager through a small CLI run
over ssh to the Pi (`ssh pi .../tools/rigctl lease acquire ...`), the same transport the manager uses
to drive the rig. The manager's HTTP API stays on Pi loopback.

## Status

`GET /v1/manager/status`, one row per GPU:

| Field | Meaning |
|---|---|
| `gpu`, `class`, `vram_mb` | inventory from the rig |
| `lease` | controller, owner, run_id, model, port, expires_at (or none) |
| `observed` | VRAM used, utilization, container, model served on the lease port |
| `verdict` | `free`, `leased`, `loading`, `unleased_busy` (something runs without a lease), `stale` (expired lease, runtime alive), `lease_idle` (leased, nothing running) |

`unleased_busy` is how a launcher that skipped the process shows up during migration.

## Interactive (Roo) through the manager

The gateway is a manager client like any other: for a cold profile it asks for an `interactive`
lease with the profile's `gpus_needed`, then has the rig launch on the granted GPUs and port, then
proxies. Unload releases the lease. Idle auto-unload (release after N minutes without requests) lands
here later.

## Migration

| Phase | Scope | Behavior change |
|---|---|---|
| A (now) | Manager in this repo: lease API as sole writer, placement, port rule, admission, observed status with `unleased_busy`. Gateway loads go through it; `rig start remote` takes `--gpus/--port/--lease` from the manager instead of choosing. `rigctl` CLI | Roo path only. Everything else is reported, not blocked |
| B | Orchestration: brain and workers acquire and release through `rigctl` instead of writing `gpu_leases.py` directly; `rig start orchestration/bench` become manager requests | Plans run under leases |
| C (after the current benches) | Benchmarking: `run_campaign.py`, `worker_lane.sh`, `phase4.sh`, `p45.sh` acquire a lease per lane and pass it to `run_runtime.sh` | Benchmarks visible and protected |
| D | Enforce: `run_runtime.sh` refuses a load without a valid lease (audit-only before this) | Nothing loads outside the process |

## Open decisions

- Default admission: all controllers on any card, with operator reservations on top (proposed),
  or reserved by class (3090 for benchmarks and interactive, 1060s for plans and benchmarks).
- Default TTLs: interactive long (renewed by the gateway while loaded), benchmark lanes renewed by
  the launcher loop, plans renewed by the worker heartbeat.
