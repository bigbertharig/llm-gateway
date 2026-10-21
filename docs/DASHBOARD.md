# Dashboard

One page for the whole setup, served by the gateway on the Pi: `http://127.0.0.1:8080/dashboard/`.
From another machine: `ssh -N -L 8080:127.0.0.1:8080 pi`, then open that address. It replaced the
orchestration dashboard (`llm_orchestration/scripts/dashboard`, port 8787) on 2026-10-20; that package
now only provides orchestration's data and controls, which this page imports.

The gateway listens on loopback only and has no API keys yet, so the page and its actions are as
reachable as `rigctl`: anyone with a shell on the Pi.

## Tabs

| Tab | Shows | Actions | Source |
|---|---|---|---|
| Rig | every card in board order (verdict, holder, temperature, fan, power against its limit, VRAM, load); claims drawn as brackets over the cards they reserve; loaded models; the rig plan; manager events | kill a card, unclaim, unload a model, cancel a plan item, load a model (cards, use, wait for busy cards, force) | the manager (`/v1/manager/status`, `docs/MANAGER.md`); actions are the manager's overrides, the same as `rigctl` |
| Models | every registry model: status, placement, roles, profiles, the best result per key test (a limit-50 run over a smoke run, then the newest), pipeline mean, decode speed and context from the latest runtime characterization, download link | none | benchmarking `model_registry.json`, `model_roles.json`, `results/model_benchmark_records.jsonl`, `results/runtime_experiments/` |
| Benchmarks | lane runs (running while the lane holds its `lane-<cards>` claim) and campaign runs with their block states | none (start runs as before) | `logs/benchmarks/worker_lanes_*`, `logs/benchmarks/campaigns/history/*/*/status.json` on the drive |
| Orchestration | task counts, alerts, workers, active batches | start a plan, resume, kill, show outputs, kill all active plans, clean up stale task files, requeue a worker's tasks | llm_orchestration `scripts/dashboard` (`summarize`, `Controls`), imported from `~/llm_orchestration` |

The old dashboard's GPU-level controls (`clear_runtime`, `reset_gpu`, `return_default`, remote runtime
cleanup) are gone: they acted on the rig outside the manager. Kill or unload through the Rig tab, then
requeue that worker's tasks on the Orchestration tab.

## Code

| File | Owns |
|---|---|
| `src/llm_gateway/dashboard/routes.py` | the page at `/dashboard/` and its API under `/dashboard/api/` (`rig`, `rig/{kill,unclaim,unload,cancel,load}`, `models`, `benchmarks`, `orchestration`, `orchestration/options`, `orchestration/{start_plan,resume_plan,kill_plan,kill_all_active,batch_outputs,cleanup_stale,requeue_worker}`) |
| `src/llm_gateway/dashboard/sources.py` | the Models, Benchmarks and Orchestration readers (plain functions over files; the routes run them in a thread) |
| `src/llm_gateway/dashboard/static/index.html` | the page: one file, no build step, no dependencies beyond two web fonts |
| `tests/test_dashboard.py` | page, rig API and overrides, both summaries, orchestration through a stand-in package |

Settings (environment, all optional): `ORCHESTRATION_ROOT` (default `~/llm_orchestration`),
`ORCHESTRATION_CONFIG` (default `<shared>/agents/config.json`), `SHARED_ROOT` (default the first of
`/media/bryan/shared`, `/mnt/shared`), `BENCHMARKING_ROOT` (as for model loads).

## Next

- An Anthropic-compatible `/v1/messages` on the gateway, so Anthropic-API agents can use local models
  (seen in RigUp, 2026-10-20).
- New releases from the publishers in the registry's `source_url`s, for NEW_MODEL_INTEGRATION Phase 0.
- API keys for the gateway before anything other than loopback can reach it.
