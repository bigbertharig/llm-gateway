# Powerful gateway host and local child agents

Status: single-rig cloud-to-local path live, 2026-11-14. The Pi remains the live controller. Multi-rig placement remains a design target.

## Target placement

A powerful GPU machine can be the main entrance to the system. It can run the gateway, an agent harness, and the rig hardware layer for its own GPUs. The same gateway can coordinate a weaker remote rig. Each rig retains its own GPU leases, plan, health checks, and runtime enforcement. The controller chooses a rig and a placement from each rig's report; model splits stay within one rig.

The current gateway manages one rig. Reaching this target requires a rig list, per-rig observation and model-port connections, placement across rigs, and the rig hardware interface on the powerful host. The Pi may remain a storage and network host even after controller duties move.

## Coding-session entry point

The dashboard should let the operator choose a cloud Codex parent or a local parent and open a durable coding session tied to a workspace. The harness owns tools, context, child sessions, task limits, and receipts. The gateway owns model offers, readiness, leases, and GPU placement. A parent asks for a `coding` or `research` local child by capability and task; it does not choose a GPU.

The existing dashboard has two separate paths: basic local model chat without harness tools, and Codex app-server coding sessions. The harness has a tested `delegate_task` MCP adapter for a local parent and now exposes `delegate_local_task` to a cloud Codex parent. The gateway resolves a child route without reserving a parent GPU. It can admit a ready child or load an offered child under its capacity and lease rules. The harness starts the child, bounds its time and token budget, retains a receipt, and returns its answer to the parent.

The first integration exposes `delegate_local_task` to Codex dashboard sessions. On 2026-11-13 a live cloud-parent/local-child round trip returned `42` from route `gpu-rig/harness-ornith-9b`, exited successfully, and wrote a child receipt. On 2026-11-14 the cloud tool gained `list_local_agents` and an optional `qwen35_shared_1060` candidate. The catalog comes from `GET /v1/manager/local-agent-capabilities`: `coding` and `research` roles, a manager-selected default local model, and the experimental shared 35B candidate. A dashboard chat can ask for local help by role without choosing a GPU. The harness starts a separate no-tools child session, keeps a per-child receipt, and releases ephemeral shared chats. The gateway manager resolves routes and asks rig orchestration to cold-start shared 35B weights when needed; the rig chooses a free worker card. The dashboard chat never sends load commands or card IDs.

The bridge accepts Codex's MCP tool permission request automatically only when the exact server and tool name have `automatic` approval in the gateway-imported harness catalog and the request has the expected form. This configured tool does not prompt the operator each time. When a dashboard user says "local agent(s)", the bridge adds a routing note to the model turn so the phrase maps to these MCP tools instead of Codex's separate cloud subagents. Other Codex command and file approvals keep their existing behavior. The existing child runner has no tools; it can help with bounded reasoning or code review, but a child that edits files needs a separate workspace-scoped tool policy and explicit tests. The child is a bounded one-task session, not a durable conversational teammate yet. Later UI work can show the child as a nested activity item with route, status, result, and receipt, alongside files and diffs so the session feels like a coding workspace.

The 2026-11-14 cold-start dashboard test used SearXNG search and Pi CPU page fetching, delegated a Nature enhanced-weathering assessment through `local_agents.delegate_local_task` to `gpu-rig/shared-35b-child-bc1d7fd3be794cce`, and returned an evidence verdict. The child receipt reports GPU1 selected by orchestration, `qwen3.6:35b-a3b-xl`, exit 0, stable roster, 4.24 seconds of child work, and successful release. The dashboard activity endpoint still returned an empty event list after completion; the child receipt is the reliable invocation record for this test.

For an explicit dashboard request, enter `/local-agent research <task>` or `/local-agent coding <task>` in a Codex conversation. The browser sends an exact `local_agents.delegate_local_task` instruction with the role and task; model and card selection remain behind the tool. A live explicit research request without a candidate used the ready shared-35B route and returned a 3.67-second child receipt. A plain "use local agents" request passed the 2026-11-14 discovery/delegation smoke after the dashboard routing note was added: it called `list_local_agents`, `search_web`, `fetch_page`, and `delegate_local_task`, then returned a shared-35B child result. Both requests still pass through the cloud model's tool decision; the slash command removes ambiguity in the requested tool name and arguments, but the browser does not directly execute the harness tool.

## Tool catalog handoff

The harness owns both dashboard MCP servers and publishes `tools/dashboard_tool_catalog.json`. The gateway imports that list with `python tools/sync_harness_tools.py --source ../llm-harness/tools/dashboard_tool_catalog.json what-new`, `sync`, or `check`. The dashboard bridge reads only its local imported `src/llm_gateway/dashboard/tool_catalog.json` for enabled tools and exact permission requests. Publish new tools as `disabled`, review their behavior and boundary policy, then switch to `automatic` and sync. A new MCP server also needs a gateway launch configuration. `list_local_agents` is a tool that reports agent roles and model offers from the manager, not the tool catalog. See the adjacent cross repo communication contract for the ownership and runtime flow.

## Network boundary

The Codex model connection uses the internet. The `local_agents` MCP adapter is started by the Codex app-server as a local stdio process on the gateway host. It calls the gateway's loopback API; the gateway reaches the rig over the private SSH connection. Tool arguments and results are visible to the cloud model as part of the session. The Codex command sandbox requests `networkAccess: false`, but MCP processes run on the host side, so that setting alone does not enforce MCP egress. Each additional MCP tool needs its own network policy and explicit allowlist. Host firewall or process isolation is needed for a hard outbound network boundary.

## Milestones

1. Cloud-parent child-route contract in the gateway, with ready and cold paths, admission failures, and a durable child receipt.
2. Harness MCP adapter for Codex cloud parents, registered only for dashboard coding sessions. Tool discovery and one bounded live call passed on 2026-11-13.
3. Dashboard activity for child start, progress, and result; workspace file and diff navigation.
4. Workspace-scoped child coding tools and approvals, then local-parent and cloud-parent parity.
5. Move the gateway/harness to a powerful host and add multi-rig placement after the single-rig coding session is proven.
