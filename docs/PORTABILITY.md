# Gateway installation boundaries

> Runtime ownership updated November 25: Orchestration owns local model decisions and its controller. Gateway is a request client. Earlier controller-design discussion below is historical; current setup and migration are in [local runtime requests](MANAGER.md).

Gateway owns its dashboard, conversations, authenticated remote ingress, cloud accounts,
connection configuration and operator policy. See the [product boundary](../README.md#product-boundary)
and [architecture](../ARCHITECTURE.md). This guide describes Gateway installation inputs.

## Active integrated deployment

Gateway can run on the rig host or a separate controller. Big Bertha uses a Pi, SSH/loopback
forwarding and distinct LAN/mobile authentication. Addresses, hosts, credentials and mounts
are private site configuration; use `deploy/site.environment.example` as a field reference.
The real service environment remains outside Git. Some deployment scripts still contain
site assumptions and must be reviewed before use on another host/network.

For local models, configure the supported Orchestration client and import reviewed model
evidence according to [the producer's artifact contract](https://github.com/bigbertharig/benchmarking/blob/master/MODEL_LIBRARY_CONTRACT.md).
Orchestration owns physical admission and executable runtime settings. Gateway requests
capabilities/outcomes and presents specific refusals. Run fake-rig contracts before a
separate live load/release, failure/recovery and concurrency check. Hardware/backend setup
belongs in [Orchestration's guide](https://github.com/bigbertharig/llm_orchestration/blob/main/docs/PORTABILITY.md).

## Tool and engine connections

Gateway consumes reviewed MCP catalogs and applies its Chat/Coding grants. Several active
launchers resolve scripts under `HARNESS_TOOLS_DIR` (default `~/llm-harness/tools`) or
per-script overrides. This is deployment coupling awaiting supported tool packages/services.
Host-control targets come from private configuration. Missing optional providers must
only disable their capabilities in the Planned portable installation.

Gateway keeps conversation state, credentials and approval records. Tool wrappers invoke
its authenticated operations, and Gateway checks authorization at execution. Harness owns
its tool/engine implementation and run journal. The Planned
[run/engine contract](https://github.com/bigbertharig/llm-harness/blob/main/docs/ENGINE_ADAPTERS.md)
defines ordered events and terminal receipts; Gateway is one consumer and keeps its own
conversation projection. Generic engine substitution is not yet a deployed capability.

## Standalone cloud coding (Planned)

Cloud dashboard/conversations must start without Rig Manager, local model files or a sibling
checkout. Today `app.py` mounts those routes only when Manager is enabled. Split the services,
background tasks and UI capability state before calling standalone startup supported.

Acceptance uses a fake cloud provider and declared tool installation to exercise authenticated
coding, streaming, cancellation, restart and unavailable optional integrations. Account login
and live provider trials are separate deployment evidence. Track the remaining work in
[implementation status](../README.md#implementation-status).
