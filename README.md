# llm-gateway

OpenAI-compatible endpoint for Big Bertha rig models. Clients (Roo in VS Code) pick a model
from `/v1/models`. The first request for a model makes the rig load it (`rig start remote <id>`)
and holds the request until it's ready. Later requests go straight through.

Cloud models are not routed here. Roo uses its native OpenRouter provider for those.

```text
Roo "local" profile -> 127.0.0.1:8080 (desktop) --ssh tunnel--> rig 127.0.0.1:8080 llm-gateway
                                                                     |  rig status --json / rig start remote <id>
                                                                     `-> 127.0.0.1:<profile port> llama-server
```

## Models

Everything comes from `remote_profiles` in the benchmarking repo's `model_roles.json`, resolved
against `model_registry.json` by that repo's `scripts/active/model_library.py`. Each profile is
published as `gpu-rig/<id>` with its `context_length`. Both files are re-read when they change,
so adding or changing a profile needs no restart. Placement (which GPUs, which port) is decided
by `rig`.

## API

| Route | Notes |
|---|---|
| `GET /v1/models` | one entry per remote profile |
| `POST /v1/chat/completions`, `POST /v1/completions` | streaming and non-streaming. Body forwarded unchanged |
| `POST /tokenize`, `POST /detokenize` | llama-server tokenizer, routed by `model`, e.g. `{"model": "gpu-rig/chat-27b", "content": "..."}`. Exact counts for compaction |
| `GET /v1/rig/status` | `rig status --json` passthrough |
| `GET /health` | liveness |

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`:

| Situation | Status |
|---|---|
| unknown model | 404 (message lists published ids) |
| prompt larger than the profile's context | 400 `context_length_exceeded`, OpenAI wording, so harnesses compact and retry |
| rig busy in another mode (orchestration/bench) | 503 `rig busy: ...` |
| not enough free GPUs for the profile | 503 with the loaded profiles |
| load failed | 502 with the rig error |
| load exceeded `START_TIMEOUT_SECONDS` | 504 |

Concurrent cold requests for one profile share a single load.

## Configuration (environment)

| Variable | Default | Notes |
|---|---|---|
| `RIG_COMMAND` | `rig` | on the Pi: `ssh rig rig` |
| `PROFILES_PATH` | `/home/bryan/llm_orchestration/shared/plans/shoulders/benchmarking/model_roles.json` | rig-local checkout; on the Pi: `/media/bryan/shared/plans/shoulders/benchmarking/model_roles.json` |
| `UPSTREAM_HOST` | `127.0.0.1` | host serving profile ports |
| `MODEL_PREFIX` | `gpu-rig` | |
| `GATEWAY_BIND` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | loopback only. No auth until it is LAN-exposed |
| `START_TIMEOUT_SECONDS` | `900` | |
| `REQUEST_TIMEOUT_SECONDS` | `900` | |

## Run, test, deploy

FastAPI + uvicorn + httpx (Python >= 3.11), in a per-host `.venv`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q                    # fake rig + fake llama-server
.venv/bin/python -m llm_gateway                  # run locally (env vars above)
deploy/deploy_rig.sh                             # from the Pi: rsync, sync rig venv, restart rig-gateway.service
```

On the rig it runs as `rig-gateway.service` (enabled at boot; it loads nothing until asked).

## Checking models

`tools/model_check.py` exercises each profile through the gateway the way a harness would:
load time, advertised vs served context, thinking leak, streaming, tool calling, `/tokenize`,
prompt tok/s at growing prompt sizes, and the overflow error. Reports go to `results/` (gitignored).

```bash
# on the rig (has `rig` for --exclusive):
.venv/bin/python tools/model_check.py --profiles coder-7b chat-27b
.venv/bin/python tools/model_check.py --profiles coder-30b devstral-24b gpt-oss-20b --exclusive
```

`--exclusive` unloads every other profile before each one. Needed for the 3090-only candidates,
since there's no auto-evict.

## Repo layout

- `src/llm_gateway/`: the gateway
- `tools/`: operator tools (model check)
- `deploy/`: rig systemd unit + deploy script
- `docs/harness/`: agent-harness research and backlog (parked). Upstream clones live alongside in `~/`.
