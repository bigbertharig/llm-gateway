# llm-gateway

Gateway serves Big Bertha's OpenAI-compatible model API, dashboard conversations, and model/tool policy. It runs on the Pi. The LAN dashboard uses browser client certificates; the mobile dashboard uses private Tailscale Serve. The GPU rig and workers stay behind the Pi.

Gateway coordinates model requests and dashboard work. Its Manager selects model offers and handles part of fit, placement, and loading; Orchestration enforces rig hardware admission. [Work organization](docs/WORK_ORGANIZATION_DIRECTION.md) records the remaining ownership migration.

## Project map

Big Bertha runs across a Pi control host, an isolated GPU rig, and optional CPU workers. Four repositories divide the system's work:

| Repository | Responsibility |
|---|---|
| [llm-gateway](https://github.com/bigbertharig/llm-gateway) | Serves the dashboard, conversations, model API, tool policy, and work coordination. Its Manager also coordinates some model placement and loading. |
| [llm_orchestration](https://github.com/bigbertharig/llm_orchestration) | Observes and controls rig hardware, enforces admission and reservations, manages runtimes, and runs the legacy brain/worker batch engine. |
| [benchmarking](https://github.com/bigbertharig/benchmarking) | Measures models and runtimes; publishes the model library, roles, load recipes, evaluation suites, and results. |
| [llm-harness](https://github.com/bigbertharig/llm-harness) | Builds and tests shared tools, agent loops, context, sessions, delegation, and recovery. Harness selection for dashboard work remains open. |

Benchmarking publishes a validated model library. Gateway imports it, serves model routes, and manages dashboard work. Harness supplies reviewed tool schemas and tests model interactions through Gateway routes. Orchestration enforces rig hardware rules and runs admitted model services. [The work-organization guide](https://github.com/bigbertharig/llm-gateway/blob/main/docs/WORK_ORGANIZATION_DIRECTION.md) tracks the remaining Manager-to-Orchestration migration.

Private site configuration supplies host addresses, credentials, model files, and mounts. The published examples use role placeholders. For another card mix, run a hardware survey and validate fit, context, concurrency, and speed before offering a load recipe. [Porting guide](https://github.com/bigbertharig/llm-gateway/blob/main/docs/PORTABILITY.md).

## Read this repo

- [Dashboard](docs/DASHBOARD.md): operator UI, saved conversations, and its local controls.
- [Remote access security](docs/REMOTE_ACCESS_SECURITY.md): why the phone route is separate from the LAN dashboard.
- [Cloud connectors](docs/CLOUD_CONNECTORS.md): Google and Claude setup, tool grants, and credential boundaries.
- [Porting to another rig](docs/PORTABILITY.md): what is reusable, what must be supplied, and what must be remeasured.
- [Manager](docs/MANAGER.md): current model admission and rig-control interface.
- [Work organization](docs/WORK_ORGANIZATION_DIRECTION.md): current ownership and the planned hardware-boundary migration.

## Documentation index

| Doc | For |
|---|---|
| [EXTERNAL_REPOS.md](EXTERNAL_REPOS.md) | outside runtimes and source projects, local revisions, and update checks |
| [docs/MANAGER.md](docs/MANAGER.md) | the entry point: the two layers (Pi manager, rig hardware), the rules, model loads and fit, reservations and card collecting, runs, cancel by owner and demand, and "Pick up here" |
| [docs/WORKLOAD_COORDINATION.md](docs/WORKLOAD_COORDINATION.md) | migration from exclusive rig modes to gateway-coordinated requests and explicit commands |
| [docs/DESIGN_NOTES.md](docs/DESIGN_NOTES.md) | design notes and ideas: failure recovery, slots, sharing capacity (exploratory) |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | the dashboard |
| [docs/REMOTE_ACCESS_SECURITY.md](docs/REMOTE_ACCESS_SECURITY.md) | private phone access boundaries, reusable setup, and verification |
| [docs/MOBILE_DASHBOARD.md](docs/MOBILE_DASHBOARD.md) | mobile dashboard and Tailscale rollout |
| [docs/POWERFUL_GATEWAY_AND_LOCAL_AGENTS.md](docs/POWERFUL_GATEWAY_AND_LOCAL_AGENTS.md) | powerful gateway host target and cloud Codex to local child agent integration |
| [docs/multi_rig_tests.md](docs/multi_rig_tests.md) | tests across two machines (network splits, failover) |

```text
client -> Pi 127.0.0.1:8080 llm-gateway (pi-gateway.service)
             |  ssh <rig-ip> rig observe / plan / lease / claim / run ...
             `-> Pi 127.0.0.1:<port> --pi-rig-tunnel.service--> rig 127.0.0.1:<port> llama-server
```

The core Gateway API and rig runtimes remain on loopback or the isolated rig link. The trusted home-LAN browser uses its separate certificate front door; the approved phone uses the narrower, tailnet-only mobile front door. See [remote access security](docs/REMOTE_ACCESS_SECURITY.md) for the boundary and [local dashboard access](docs/LOCAL_DASHBOARD_ACCESS.md) for LAN setup. The inference API offers local models; dashboard Codex conversations have their own provider and tool policy.

## rigctl: the common commands

```bash
tools/rigctl.py status                                  # every GPU, its lease or claim, the plan, recent events
tools/rigctl.py fit MODEL [--cards N | --gpus 1,3]      # would it fit, at what context and split (nothing loads)
tools/rigctl.py load-model MODEL [--gpus 1,3] [--wait S|forever] [--owner WHO]
                                                        # no --gpus: the harness picks the cards; with --wait it
                                                        # collects them as they free (MANAGER.md, Reservations)
tools/rigctl.py unload PROFILE
tools/rigctl.py run-start --run ID [--resume "CMD"] [--auto-resume] [--needs MIB[xN]] -- CMD...
tools/rigctl.py runs | recover RUN | run-stop RUN | dismiss RUN
tools/rigctl.py cancel-owner WHO                        # stop everything one owner has: runs, loads, leases, claims, waits
tools/rigctl.py claim NAME --gpus 1,3 | unclaim NAME | claims
tools/rigctl.py kill --gpus 1,3                         # operator override: stop every runtime on those cards
```

## Models

The gateway runs from its own files and never reads the benchmarking repo at runtime (ARCHITECTURE.md,
"Boundaries"). `tools/generate_profiles.py` is its importer: it reads the benchmarking model library
(where benchmarking keeps what it learned about how each model loads) and writes, both committed here:

- `model_catalog.json`: every model with its resolved profiles, the roles, the runtime image and the
  dashboard's model summary; the manager plans and loads from it.
- `remote_profiles.json`: the offer. `OFFER` in the script names each published profile, its model, the
  registry profile to use (usually `interactive`) and a description. It names no cards: the manager's fit
  places each profile and plans the largest context that fits, at least the profile's `ctx_size`.

```bash
tools/generate_profiles.py            # after an OFFER edit or a library change (run on the Pi)
tools/generate_profiles.py --check    # exit 1 if an offer is invalid or either file is stale
```

The gateway publishes each profile as `gpu-rig/<id>` with its `context_length`, and re-reads the
files when they change, so no restart is needed. Offers may use `active` or `candidate` registry models.
What the manager learns from loads goes to `lessons/` (see `lessons/README.md`).

## API

| Route | Notes |
|---|---|
| `GET /v1/models` | one entry per remote profile |
| `GET /v1/rig/ready-roster` | read-only, versioned routes that are both offered and already ready; includes actual served context |
| `POST /v1/chat/completions`, `POST /v1/completions` | streaming and non-streaming. Body forwarded unchanged |
| `POST /v1/responses` | Responses API passthrough for clients such as Codex; requires a loaded llama-server that implements this route |
| `POST /tokenize`, `POST /detokenize` | llama-server tokenizer, routed by `model`. Exact counts for compaction |
| `GET /v1/rig/status` | rig status plus `gateway_loading`: loads in progress with `since` and `elapsed_s` |
| `GET /health` | liveness |
| `/v1/manager/...` | the rig manager: `status`, `leases`, `claims`, `models/fit`, `models/load`, `profiles/<id>/load\|unload`, `runs` (start, `recover`, `stop`, `dismiss`), `owners/<who>/cancel`, `kill`, `plan`, `controller`, `reconcile`. Rules: [docs/MANAGER.md](docs/MANAGER.md) |
| `/dashboard/` | the dashboard and its `api/` routes |

Harnesses should read `GET /v1/rig/ready-roster` before inference. Its `data` contains only
published profiles whose rig status is `ready`, whose served model identity matches the published
model, and whose actual `ctx_size` and port are known. Each entry gives the gateway route `id`,
underlying `model`, `registry_profile` when available, `context_length` from the running server,
and `/tokenize` as `tokenizer_path`. `output_limit` and `tool_call_format` are `null` until those
capabilities have verified sources. An empty `data` array means no currently usable route; this
read does not start or alter a model. `GET /v1/models` remains the broader published offer and its
context value may differ from the running server's context.

For a passive harness, send `X-Require-Ready: true` on every completion, Responses, and tokenizer request.
The gateway then checks current readiness and returns HTTP 409 `model_not_ready` if the route is
no longer ready. Without that header, the existing cold-load behavior remains in effect. A ready
check and inference are separate operations, so a model that stops between them can still produce
an upstream error, but the ready-only request will not initiate a load.

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`:

| Situation | Status |
|---|---|
| unknown model | 404 (message lists published ids) |
| prompt larger than the profile's context | 400 `context_length_exceeded`, OpenAI wording, so harnesses compact and retry |
| not enough free GPUs, or cards kept free for waiting work | 503 / 409 with the reason |
| load failed | 502 with the rig error |
| rig unreachable (ssh exit 255) | 502 `rig unreachable over ssh: ...` (waits keep waiting through an outage) |
| load exceeded `START_TIMEOUT_SECONDS` | 504; the load keeps running on the rig |
| a wait cancelled (its claim removed, or `cancel-owner`) | 409 `cancelled` |
| API keys configured, key missing or wrong | 401 `invalid_api_key` |

Concurrent cold requests for one profile share a single load.

## Configuration (environment)

| Variable | Default | Notes |
|---|---|---|
| `RIG_COMMAND` | `rig` | Pi service: multiplexed `ssh ... <rig-ip> /usr/local/bin/rig` (see `deploy/pi-gateway.service`). ssh exit 255 maps to 502 "rig unreachable" |
| `PROFILES_PATH` | `remote_profiles.json` in this repo | generated by `tools/generate_profiles.py` |
| `UPSTREAM_HOST` | `127.0.0.1` | host serving profile ports (on the Pi: its end of `pi-rig-tunnel.service`) |
| `MODEL_PREFIX` | `gpu-rig` | |
| `GATEWAY_BIND` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | a non-loopback bind is refused unless API keys are configured |
| `GATEWAY_API_KEYS_FILE` | `/etc/llm-gateway/api_keys` | one bearer key per line (`#` comments). Missing file = auth off |
| `RIG_MANAGER` | `off` | Pi service: `on` |
| `START_TIMEOUT_SECONDS` | `900` | |
| `REQUEST_TIMEOUT_SECONDS` | `900` | |

## Run, test, deploy

FastAPI + uvicorn + httpx (Python >= 3.11), in a per-host `.venv`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                    # fake rig + fake llama-server
.venv/bin/python -m llm_gateway                  # run locally (env vars above)
deploy/deploy_pi.sh                              # on the Pi: check profiles, copy repo to the rig, restart Pi services
```

On the Pi it runs as `pi-gateway.service`, with `pi-rig-tunnel.service` forwarding the rig's
model ports 11434-11441 to Pi loopback (both enabled at boot; nothing loads until asked). The deploy
script restarts only when no load is in flight. On Windows (another controller machine):
`deploy\run_windows.ps1`, with the Pi's gateway stopped first: one controller at a time.

A load is never killed by the gateway: past `START_TIMEOUT_SECONDS` the request gets a 504 and the
load continues on the rig; the next request finds it ready.

## Tools

| Tool | What it does |
|---|---|
| `tools/rigctl.py` | the manager's CLI (above) |
| `tools/generate_profiles.py` | the offer -> `remote_profiles.json` |
| `tools/model_check.py` | exercise each profile the way a harness would: load time, context, thinking leak, streaming, tool calls, tokenize, prompt speed, overflow error. Reports in `results/` (gitignored) |
| `tools/role_model.py` | resolve a benchmarking role to a model |
| `tools/rig_stress.py` | power and thermal stress test: one model per card, ramp, hold, optional reload spikes (`--spikes`), factory limits until the next boot (`--uncap`); per-card temperature, fan, power, clock by GPU UUID, `--compare` against an earlier run |
| `tools/power_cap_sweep.py` | speed and power of one card across a series of power caps (decode and prompt tok/s, J per token) |
| `tools/split_sweep.py`, `tools/pcie_split_test.py` | split-load speed across cards and PCIe link settings |

What these tools have found, where their results are kept and which question each answers: benchmarking `RIG_PERFORMANCE.md`; the measurements: benchmarking `results/RUNTIME_REFERENCE.md`.

| `tools/multi_rig_tests.py`, `tools/dual_rig_bench.sh` | the tests in `docs/multi_rig_tests.md` |

## Repo layout

- `src/llm_gateway/`: the gateway, the manager (`manager/`) and the dashboard (`dashboard/`)
- `tools/`: operator tools (above)
- `remote_profiles.json`: generated offer
- `deploy/`: Pi systemd units and the deploy script
- `docs/`: see "Start here"
