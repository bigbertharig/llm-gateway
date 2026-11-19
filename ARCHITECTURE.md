# Architecture

This is the current operator-facing architecture of the integrated Big Bertha project. The four repositories can be tested separately, but the deployed service depends on their published artifacts and live interfaces. For operational detail, see [the Manager guide](docs/MANAGER.md), [the dashboard guide](docs/DASHBOARD.md), and [work organization](docs/WORK_ORGANIZATION_DIRECTION.md).

## Ownership and data flow

1. **Benchmarking** measures models and runtimes and publishes a validated, hash-checked model library. A load recipe is evidence until it is reviewed and promoted into a supported offer. It does not schedule live user work.
2. **Gateway** imports that library, presents the dashboard and OpenAI-compatible API, keeps conversations and tool grants, and coordinates model requests. Its current Manager still makes some fit, placement, lease, and load decisions while the hardware boundary is being migrated.
3. **Orchestration** observes the rig, enforces hardware admission and reservations, starts/stops admitted runtimes, and reports status. Its brain/worker Markdown-plan queue remains for legacy batch jobs; it is not the planned owner of new dashboard work organization.
4. **Harness** develops and tests the model-use loop: shared tools, prompts, sessions, context, delegation, limits, and recovery. Gateway consumes its reviewed tool catalog. No candidate harness has been promoted as the sole dashboard work engine.

The Gateway offers model routes, including cold offers. A route is *ready* only when the rig reports the matching running service, context, and port. The gateway's model list must not be read as a list of loaded models. The Manager proposes a fit and route; the rig may refuse it when live leases, reservations, guards, or runtime state conflict. The rig's admission and reservation record is authoritative for physical capacity. Clients may request a ready-only response to avoid waiting for a cold load; concurrent requests for the same cold offer use the Manager's single-flight load path.

## Hardware and deployment boundary

The current deployment puts Gateway on a Pi and keeps rig runtimes behind SSH and loopback forwarding. The LAN dashboard, the narrower mobile Tailscale front door, and the model API have distinct access rules; see [remote access security](docs/REMOTE_ACCESS_SECURITY.md). Addresses, credentials, model archives, and host inventory are private site configuration. The published docs are not a turnkey deployment template.

Model IDs and routes do not encode a physical card. Fit and execution still depend on observed VRAM, supported runtime/backend, host memory or storage for tiered loads, and a tested context/concurrency recipe. Some published experiments deliberately pin cards or use a private runtime fork; those measurements do not establish that the same load works on every card mix. See [porting to another rig](docs/PORTABILITY.md) and Benchmarking's [load and chat placement evidence](https://github.com/bigbertharig/benchmarking/blob/master/MODEL_LOADING_AND_CHAT_PLACEMENT.md).

## Current migration

Gateway's Manager and Orchestration currently share some admission and placement responsibilities. The intended boundary moves complete hardware reservations and placement to Orchestration while Gateway retains user-facing work policy and model choice. This document describes the current mixed state, not a promise that the migration is complete. New work graphs and model assignment belong to Gateway; legacy Markdown plans remain a separate compatibility path.
