# Powerful gateway host and local child agents

Status: design target, 2026-11-13. The Pi remains the live controller. This note records the proposed placement and the first dashboard/harness integration.

## Target placement

A powerful GPU machine can be the main entrance to the system. It can run the gateway, an agent harness, and the rig hardware layer for its own GPUs. The same gateway can coordinate a weaker remote rig. Each rig retains its own GPU leases, plan, health checks, and runtime enforcement. The controller chooses a rig and a placement from each rig's report; model splits stay within one rig.

The current gateway manages one rig. Reaching this target requires a rig list, per-rig observation and model-port connections, placement across rigs, and the rig hardware interface on the powerful host. The Pi may remain a storage and network host even after controller duties move.

## Coding-session entry point

The dashboard should let the operator choose a cloud Codex parent or a local parent and open a durable coding session tied to a workspace. The harness owns tools, context, child sessions, task limits, and receipts. The gateway owns model offers, readiness, leases, and GPU placement. A parent asks for a `coding` or `research` local child by capability and task; it does not choose a GPU.

The existing dashboard has two separate paths: basic local model chat without harness tools, and Codex app-server coding sessions. The harness has a tested `delegate_task` MCP adapter for a **local parent** plus a gateway child-route resolver. The cloud Codex dashboard path does not yet expose that adapter, and the adapter currently requires a local parent route. A cloud parent needs a child-route request that does not reserve a parent GPU. The gateway should admit a ready child or load an offered child under the same capacity and lease rules, then return its actual route and context. The harness should start the child, bound its time and token budget, retain a receipt, and return its answer to the parent.

The first integration should expose a clearly named `delegate_local_task` tool to a Codex dashboard session and prove one cloud-parent/local-child round trip. It should preserve the current workspace sandbox and approval behavior. The existing child runner has no tools; it can help with bounded reasoning or code review, but a child that edits files needs a separate workspace-scoped tool policy and explicit tests. Later UI work can show the child as a nested activity item with route, status, result, and receipt, alongside files and diffs so the session feels like a coding workspace.

## Milestones

1. Cloud-parent child-route contract in the gateway, with ready and cold paths, admission failures, and a durable child receipt.
2. Harness MCP adapter for Codex cloud parents, registered only for dashboard coding sessions. Prove tool discovery and one bounded live call without changing the rig's existing work.
3. Dashboard activity for child start, progress, and result; workspace file and diff navigation.
4. Workspace-scoped child coding tools and approvals, then local-parent and cloud-parent parity.
5. Move the gateway/harness to a powerful host and add multi-rig placement after the single-rig coding session is proven.
