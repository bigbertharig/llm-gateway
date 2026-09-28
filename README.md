# llm-gateway

Private LAN gateway for using OpenRouter and Big Bertha Rig models through one
OpenAI-compatible endpoint in VS Code.

The gateway runs on the Pi. VS Code connects to the Pi, selects a model with an
`openrouter/` or `local/` prefix, and does not need to know about GPU placement
or worker ports.

## Current Status

Implemented:

- bearer authentication for all `/v1` routes;
- `GET /health`;
- combined `GET /v1/models` catalog;
- non-streaming and streaming `POST /v1/chat/completions` forwarding;
- curated OpenRouter model selection;
- local model acquire, renewal, forwarding, and release contract.

The local path requires the matching API in `llm_orchestration`. Until that is
installed, cloud routing can work independently and local discovery reports
unavailable.

## Install

```bash
cd /home/bryan/llm-gateway
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

Set unique gateway and orchestrator tokens in `.env`. Do not commit that file.

Add preferred OpenRouter IDs to `config/models.json`:

```json
{
  "schema_version": 1,
  "openrouter_models": [
    "provider/model-id"
  ]
}
```

## Run on the Pi

```bash
.venv/bin/uvicorn llm_gateway.main:app \
  --env-file .env \
  --host 0.0.0.0 \
  --port 8080
```

Restrict TCP port 8080 to the main machine on the home LAN. Do not configure
router port forwarding for the gateway, orchestrator, or worker ports.

## VS Code Provider

```text
Base URL: http://PI_LAN_ADDRESS:8080/v1
API key:  value of GATEWAY_API_KEY
```

The extension should populate its selector from `GET /v1/models`.

## Test

```bash
.venv/bin/pytest
```
