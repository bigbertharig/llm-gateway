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

Everything comes from `remote_profiles` in
`shared/plans/shoulders/benchmarking/model_task_library.json`. Each profile is published as
`gpu-rig/<id>` with its `context_length`. The file is re-read when it changes, so adding or
changing a profile needs no restart. Placement (which GPUs, which port) is decided by `rig`.

## API

| Route | Notes |
|---|---|
| `GET /v1/models` | one entry per remote profile |
| `POST /v1/chat/completions`, `POST /v1/completions` | streaming and non-streaming. Body forwarded unchanged |
| `GET /v1/rig/status` | `rig status --json` passthrough |
| `GET /health` | liveness |

Errors use the OpenAI shape `{"error": {"message", "type", "code"}}`:

| Situation | Status |
|---|---|
| unknown model | 404 (message lists published ids) |
| rig busy in another mode (orchestration/bench) | 503 `rig busy: ...` |
| not enough free GPUs for the profile | 503 with the loaded profiles |
| load failed | 502 with the rig error |
| load exceeded `START_TIMEOUT_SECONDS` | 504 |

Concurrent cold requests for one profile share a single load.

## Configuration (environment)

| Variable | Default | Notes |
|---|---|---|
| `RIG_COMMAND` | `rig` | on the Pi: `ssh rig rig` |
| `PROFILES_PATH` | `/mnt/shared/plans/shoulders/benchmarking/model_task_library.json` | Pi: `/media/bryan/shared/...` |
| `UPSTREAM_HOST` | `127.0.0.1` | host serving profile ports |
| `MODEL_PREFIX` | `gpu-rig` | |
| `GATEWAY_BIND` / `GATEWAY_PORT` | `127.0.0.1` / `8080` | loopback only. No auth until it is LAN-exposed |
| `START_TIMEOUT_SECONDS` | `900` | |
| `REQUEST_TIMEOUT_SECONDS` | `900` | |

## Run, test, deploy

Standard library only (Python >= 3.11). No venv or install needed.

```bash
python3 -m unittest -v tests/test_gateway.py     # fake rig + fake llama-server
PYTHONPATH=src python3 -m llm_gateway            # run locally
deploy/deploy_rig.sh                             # from the Pi: rsync to rig, (re)start rig-gateway.service
```

On the rig it runs as `rig-gateway.service` (enabled at boot; it loads nothing until asked).
