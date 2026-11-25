# llm-gateway

Gateway provides the dashboard, saved conversations, cloud accounts and model connections,
authenticated remote access, operator approvals, and connections to optional tools and services.
It runs cloud coding and chat workflows without Orchestration or a local model catalog.

Orchestration decides how local models run. Gateway requests an outcome and presents the
result. Profile selection, fit, placement, admission, leases, loading/unloading, shared-model
context allocation and recovery all live in Orchestration. Gateway alone cannot run local models.

## Install and run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m llm_gateway
```

The default is cloud-only when `ORCHESTRATION_URL` is absent. Configure cloud accounts in the
dashboard. A non-loopback bind requires configured Gateway bearer keys. See
[cloud connectors](docs/CLOUD_CONNECTORS.md) and [remote access](docs/REMOTE_ACCESS_SECURITY.md).

For local models, configure `ORCHESTRATION_URL` and `ORCHESTRATION_CONTROL_TOKEN` in the
protected service environment. The owner must support `orchestration-control/1` and use the
same model prefix. `UPSTREAM_HOST` identifies the reachable inference host/tunnel; Gateway
continues to own that network connection. `GATEWAY_MODE=cloud` explicitly disables local access.

There is no `RIG_COMMAND`, SSH command fallback, local model-file import or embedded scheduler.
An absent connection returns a specific unavailable response. An unavailable owner leaves
cloud workflows available; stale metadata cannot authorize a local model operation.

## Interfaces

| Interface | Behavior |
|---|---|
| `/dashboard/` | Cloud accounts, coding/chat conversations, optional runtime views |
| `GET /v1/models` | Cloud models and owner-published local offers; an offer is not readiness |
| `GET /v1/rig/ready-roster` | Observed, identity-matched local routes and served capacities |
| `POST /v1/chat/completions`, `/v1/responses` | Configured cloud connectors or admitted local inference |
| `POST /tokenize`, `/detokenize` | Local tokenizer forwarding after owner readiness checks |
| `/v1/manager/...` | Authenticated request facade to Orchestration's decision engine |
| `/v1/harness/...` | Optional Harness run-control connection |
| `GET /v1/capabilities` | Configured product capabilities |
| `GET /health` | Gateway liveness |

`X-Require-Ready: true` asks Orchestration to refuse cold or mismatched local routes.
`X-Request-ID` on Manager mutations preserves the operator's request identity. Gateway does
not retry uncertain writes or take over controller ownership when a connection fails.

## Documentation

- [Standalone setup and validation](docs/STANDALONE.md)
- [Architecture](ARCHITECTURE.md)
- [Local runtime request facade](docs/MANAGER.md)
- [Dashboard](docs/DASHBOARD.md), [LAN access](docs/LOCAL_DASHBOARD_ACCESS.md), [mobile access](docs/MOBILE_DASHBOARD.md)
- [Cloud connectors](docs/CLOUD_CONNECTORS.md)
- [Optional model evidence display](docs/MODEL_LIBRARY_HANDOFF.md)
- [External runtime dependencies](EXTERNAL_REPOS.md)

## Validation and rollout

Use `tools/run_test_suite.py --list` for owned API, dashboard, conversation, tool-policy and
connection suites. Runtime decision tests moved with their implementation to Orchestration.
`tools/verify_install.py --wheel FILE --receipt OUTPUT` checks a fresh installed cloud workflow.

The November 25 source extraction moves the remaining local controller out of Gateway.
Deployment requires starting the Orchestration control service before enabling this connection.
Stop the old Gateway controller and reconcile its outstanding leases before handing control to
the new service. Existing GPU-host edits and running services are not overwritten by this change.
