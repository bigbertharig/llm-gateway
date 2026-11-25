# Porting and extending the Big Bertha design

The four repositories form one system, but none should require another repository's checkout or private
Python modules at runtime. They exchange versioned artifacts, requests, events, and receipts. Gateway
can run on the rig host or on a separate controller; a Pi and a shared drive are deployment choices, not
API requirements. The reusable interfaces are the published model-evidence format, Gateway API,
reviewed tool catalog, Harness task/event/receipt boundary, and Orchestration admission/observation
contracts. Benchmarking's
[cross-repo contract](https://github.com/bigbertharig/benchmarking/blob/main/CROSS_REPO_CONTRACT.md)
is the ownership source of truth.

Big Bertha's deployment scripts still assume a Pi, an isolated rig link, private host inventory, and
particular mounts and model files. Some tracked scripts still contain site-specific addresses; audit
those paths before using them on another network. Harness MCP scripts resolve through
`HARNESS_TOOLS_DIR` (default `~/llm-harness/tools`) or per-script overrides; the host-control tool reads
its targets from a private JSON config.

## Repository boundaries

| Repository | Portable responsibility | Replaceable implementation |
|---|---|---|
| Benchmarking | Canonical model IDs, measured evidence, recipes, campaigns, and result history | Test runtimes and suites may vary; evidence keeps its hardware/runtime identity. |
| Gateway | Conversations, authentication, operator policy, model/tool assignment, work requests, and public API | Controller host and UI may vary; Gateway does not become hardware authority. |
| Orchestration | Inventory, physical admission, reservations, runtime lifecycle, observation, and hardware receipts | NVIDIA backend today; AMD and Apple integrations planned. |
| Harness | Provenance, bounded model/tool interaction, tool broker, canonical events, recovery, and terminal receipts | The custom engine is default; the engine contract supports additional bounded reasoning loops. |

Gateway asks Orchestration for capabilities and outcomes; it must not grow CUDA, ROCm, Metal, device
index, or unified-memory placement logic. Gateway supplies an effective grant to Harness; a substituted
harness engine may narrow that grant but cannot widen it or bypass approval and tool validation.

## What to supply and verify

1. Supply private host inventory, addresses, mount roots, credentials, and model files for the new site. Start with `deploy/site.environment.example` for the Pi service target; keep the real file outside Git at `/etc/llm-gateway/site/environment`. Do not copy the published examples as literal production configuration.
2. Run `rig scan` on the GPU host to refresh the rig-owned `brain/hw/hardware_inventory.json`, then run Benchmarking's [rig survey and synthetic work scan](https://github.com/bigbertharig/benchmarking/blob/master/RIG_LIMIT_TEST.md) for measured RAM, storage, and transfer behavior. `rig observe` remains the live admission input. VRAM is the first fit input; runtime support, RAM/storage headroom, and context/KV reservations can also block a load.
3. Import the reviewed [model library](https://github.com/bigbertharig/benchmarking/blob/master/CROSS_REPO_CONTRACT.md), then remeasure load, context, speed, concurrency, and quality for recipes you intend to offer. A recipe measured on Big Bertha is a candidate on another card mix, not a guaranteed placement.
4. Test Gateway's fake-rig contracts before enabling a live rig. Then test one model load and release, failure/recovery, and concurrent admission on the actual hardware. Legacy NFS queue users should separately test competing task claims and worker loss on their own NFS server and mount options.

## Accelerator strategy

Orchestration is NVIDIA/CUDA-focused today, including discovery, telemetry, power, fit constants, log
parsing, containers, and launch paths. It supports different NVIDIA card layouts through discovery and
site calibration. AMD/ROCm and Apple Silicon/Metal integrations are planned.

The design keeps one Orchestration control plane with versioned accelerator backends:

- `nvidia`: extracts today's deployed behavior without changing it;
- `amd`: planned ROCm discovery, topology, telemetry, launch, and fit behavior;
- `apple`: planned Metal and unified-memory capability and lifecycle behavior.

Forking the complete project into vendor repositories would duplicate locking, leases, fencing,
recovery, and receipt logic and would let those paths answer the same question differently. A backend
can become a separately packaged repository later if licensing, dependencies, release cadence, or
maintainership require it, provided it still implements the same versioned contract. The detailed plan
and acceptance order live in Orchestration's
[`docs/PORTABILITY.md`](https://github.com/bigbertharig/llm_orchestration/blob/main/docs/PORTABILITY.md).

## Harness strategy

The project-owned Harness remains the maintained default. Additional engines integrate through the
versioned engine boundary: Gateway provides the objective, ready route, evidence references, budgets,
and effective grants; the engine returns canonical ordered events and a terminal receipt. Gateway keeps
conversation state and policy, while the Harness boundary keeps provenance, tool validation,
approvals, durable dispatch, and terminal classification. A third-party harness is not trusted merely
because it can call the model or MCP.

The input/output fields, security invariants, and conformance gates are defined in Harness's
[`docs/ENGINE_ADAPTERS.md`](https://github.com/bigbertharig/llm-harness/blob/main/docs/ENGINE_ADAPTERS.md).

The OpenAI-compatible API alone does not make hardware scripts or harness policy portable. See
[the current architecture](../ARCHITECTURE.md), [Manager guide](MANAGER.md), and
[work-organization direction](WORK_ORGANIZATION_DIRECTION.md) for the boundaries still being migrated.

## Standalone status (2026-11-24)

Standalone cloud coding is a required product workflow, but is not yet verified.
Currently `app.py` mounts dashboard, cloud, and conversation routes only when Rig
Manager is enabled; several tool launchers also resolve scripts from a Harness
checkout. Those are open isolation gaps. See the [source audit and acceptance
criteria](https://github.com/bigbertharig/benchmarking/blob/master/reports/STANDALONE_ISOLATION_AUDIT_2026-11-24.md). The engine substitution description above is a target contract,
not a completed generic adapter/conformance implementation.
