# Architecture

`llm-gateway` is the stable LAN endpoint used by editor and agent clients.
It is a protocol gateway, not a scheduler.

```text
VS Code on the main machine
        |
        v
llm-gateway on the Pi
        |-- openrouter/<id> --> OpenRouter
        `-- local/<id> ------> localhost model API
                                  `-> llm_orchestration --> GPU worker
```

## Ownership

The gateway owns:

- client authentication;
- the OpenAI-compatible client endpoint;
- the curated OpenRouter model list;
- explicit `openrouter/` and `local/` routing;
- holding a local model session for the duration of each inference request.

`llm_orchestration` owns:

- the local model catalog;
- GPU placement and leases;
- runtime load, health, and cleanup;
- split-model mechanics;
- the endpoint and runtime model name returned for a local session.

## Local Session Contract

The expected Pi-side orchestration API is:

```text
GET    /v1/local/models
POST   /v1/local/models/{model_id}/acquire
POST   /v1/local/sessions/{session_id}/renew
DELETE /v1/local/sessions/{session_id}
```

An acquire response contains at least:

```json
{
  "session_id": "interactive-...",
  "endpoint": "http://10.0.0.3:11435/v1",
  "runtime_model": "Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf",
  "expires_at": "2026-09-28T12:00:00Z"
}
```

The gateway renews the session while inference is active and releases it when
the response or stream ends.
