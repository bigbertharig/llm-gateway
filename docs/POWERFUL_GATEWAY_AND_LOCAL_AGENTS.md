# Powerful gateway host and local child agents

Status: design target, 2026-11-13. The Pi remains the live controller. This note records the proposed placement and the first dashboard/harness integration.

## Target placement

A powerful GPU machine can be the main entrance to the system. It can run the gateway, an agent harness, and the rig hardware layer for its own GPUs. The same gateway can coordinate a weaker remote rig. Each rig retains its own GPU leases, plan, health checks, and runtime enforcement. The controller chooses a rig and a placement from each rig's report; model splits stay within one rig.

The current gateway manages one rig. Reaching this target requires a rig list, per-rig observation and model-port connections, placement across rigs, and the rig hardware interface on the powerful host. The Pi may remain a storage and network host even after controller duties move.

## Coding-session entry point

The dashboard should let the operator choose a cloud Codex parent or a local parent and open a durable coding session tied to a workspace. The harness owns tools, context, child sessions, task limits, and receipts. The gateway owns model offers, readiness, leases, and GPU placement. A parent asks for a `coding` or `research` local child by capability and task; it does not choose a GPU.

The existing dashboard has two separate paths: basic local model chat without harness tools, and Codex app-server coding sessions. The harness has a tested `delegate_task` MCP adapter for a local parent and now exposes `delegate_local_task` to a cloud Codex parent. The gateway resolves a child route without reserving a parent GPU. It can admit a ready child or load an offered child under its capacity and lease rules. The harness starts the child, bounds its time and token budget, retains a receipt, and returns its answer to the parent.

The first integration exposes `delegate_local_task` to Codex dashboard sessions. On 2026-11-13 a live cloud-parent/local-child round trip returned `42` from route `gpu-rig/harness-ornith-9b`, exited successfully, and wrote a child receipt. The bridge accepts Codex's MCP tool permission request automatically only when the server is `local_agents`, the tool is `delegate_local_task`, and the request has the expected form. This configured tool does not prompt the operator each time. Other Codex command and file approvals keep their existing behavior. The existing child runner has no tools; it can help with bounded reasoning or code review, but a child that edits files needs a separate workspace-scoped tool policy and explicit tests. Later UI work can show the child as a nested activity item with route, status, result, and receipt, alongside files and diffs so the session feels like a coding workspace.

## Network boundary

The Codex model connection uses the internet. The `local_agents` MCP adapter is started by the Codex app-server as a local stdio process on the gateway host. It calls the gateway's loopback API; the gateway reaches the rig over the private SSH connection. Tool arguments and results are visible to the cloud model as part of the session. The Codex command sandbox requests `networkAccess: false`, but MCP processes run on the host side, so that setting alone does not enforce MCP egress. Each additional MCP tool needs its own network policy and explicit allowlist. Host firewall or process isolation is needed for a hard outbound network boundary.

## Milestones

1. Cloud-parent child-route contract in the gateway, with ready and cold paths, admission failures, and a durable child receipt.
2. Harness MCP adapter for Codex cloud parents, registered only for dashboard coding sessions. Tool discovery and one bounded live call passed on 2026-11-13.
3. Dashboard activity for child start, progress, and result; workspace file and diff navigation.
4. Workspace-scoped child coding tools and approvals, then local-parent and cloud-parent parity.
5. Move the gateway/harness to a powerful host and add multi-rig placement after the single-rig coding session is proven.
