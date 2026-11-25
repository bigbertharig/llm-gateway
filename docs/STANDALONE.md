# Standalone Gateway

Set `GATEWAY_MODE=cloud` to serve dashboard, saved conversations, cloud accounts and configured
provider connectors with no local runtime profiles, Rig Manager, SSH target or model-library
requirement. Runtime routes explicitly report unavailable. `/v1/capabilities` reports the mode.
The same bearer-key middleware protects these routes; a non-loopback bind still requires keys.
Install and run using the normal package entry point and configure accounts through Cloud.

Optional tools come from an installed Harness package or explicit per-server script settings.
`HARNESS_TOOLS_DIR` is an explicit compatibility setting; there is no automatic sibling checkout
lookup for the reusable installed tools. An absent optional package removes that tool capability.
Install Harness with `python -m pip install /path/to/llm_harness.whl` in the Gateway interpreter
when using these in-process launch descriptors. Provider-native Codex/Claude Code sessions retain
their native session semantics and use independently callable MCP tools.

## Optional Harness run connection

Configure `HARNESS_SERVICE_URL` and `HARNESS_SERVICE_TOKEN`. Authenticated callers can use
`/v1/harness/runs` and its status/events/input/cancel/resume paths. Gateway transports the
`harness-control/1` envelope; Harness owns tool grants, engine execution and private journals.
A failed transport reports an uncertain result: reconcile by the original request ID before
submitting again. Unknown versions, event gaps and duplicated reconnect events have explicit
consumer checks. The connection is not required for cloud dashboard conversations.

`POST /v1/operations/restart` implements `gateway-reset/1` with `request_id`, `conversation_id`,
`confirmation` and `recovery_note`. It requires an explicit configured bearer key even on
loopback, plus `GATEWAY_RESTART_ENABLED=1`. The owner verifies the saved provider thread and
writes the durable receipt before launching the external recovery worker. Duplicate IDs with
changed arguments are refused; an interrupted scheduling attempt remains `dispatching` and
is not falsely reported as scheduled. Configure `GATEWAY_SERVICE_UNIT` and `GATEWAY_LOCAL_URL`
for the local service; the existing Pi default remains for compatibility. Harness only calls
this API and knows neither the conversation database nor the service-control implementation.

## Optional runtime connection

Set `ORCHESTRATION_URL` and `ORCHESTRATION_CONTROL_TOKEN`. Orchestration's `rig-control serve`
provides `orchestration-control/1`; its CLI/API works without Gateway. Gateway is a request
client and dashboard, with no local scheduler, profile generator, controller heartbeat or
command fallback. `MODEL_PREFIX` must match the owner's `ORCHESTRATION_MODEL_PREFIX`.
Configure `UPSTREAM_HOST` for the inference network connection separately.

The owner validates local offers, readiness, model/context identity, admission, placement,
shared-chat allocation, explicit reset/unload intent and runtime recovery. Gateway retains
conversation history and applies conversation-activity guards before requesting changes.
Owner operations have durable request IDs. Reusing an ID with changed arguments is refused;
interrupted dispatch requires reconciliation. No automatic write retry occurs in Gateway.

Deploy the owner service and reconcile old controller leases before switching this Gateway
release. Source checks use fake hardware and installed-package acceptance; they do not deploy
onto the GPU host. Independent cloud startup remains available when runtime access is absent.

Correctness suites remain here for APIs, dashboard, conversations, tool grants and connection
contracts. Hardware, fit, scheduling and importer tests moved to Orchestration with the code.

Reviewed MCP schemas are frozen in the Harness artifact with normalized SHA-256 digests.
Gateway's MCP dispatcher checks the discovered input schema against the imported digest before advertising a
tool. Unknown or changed schemas fail closed until the producer snapshot is reviewed and
imported again. Both artifact importers validate the digest before replacing their last valid
snapshot. Legacy name-only artifacts can be read for migration, but cannot authorize live
Gateway tool dispatch without a schema digest. Harness checks all ten owned MCP surfaces
using discovery-only requests (`tools/mcp_schema_snapshot.py check`).

The refreshed artifact also includes Harness's model-swap tool. Existing Gateway grants do
not gain it merely by importing that artifact: `GATEWAY_ALLOW_AGENT_MODEL_SWAP=1` is an
explicit deployment opt-in. Native external coding engines own their own MCP sessions and
must apply their own schema-version checks; dispatcher checks do not claim native-engine
substitution conformance.

For installed cloud-only acceptance, build a wheel and run
`tools/verify_install.py --wheel FILE --receipt OUTPUT`. This uses a fresh virtual
environment and HOME, a fake cloud coding bridge, packaged dashboard assets, and
saved-history recovery after an application restart. No live provider account is used.

November 25 source validation: 160 owned regression tests passed. Fresh installed packages
passed cloud coding/history acceptance, separate-environment Orchestration integration, and
the shared-chat lifecycle/conversation scenario. Gateway 0.4.0 requires the new owner service
for local models; no live controller handoff was performed during these checks.


November 25 live acceptance: the production `python -m llm_gateway` entry point
now logs current connection settings; an obsolete startup reference discovered during
the handoff was fixed. The registered API/infrastructure suites pass 47 tests, including
cloud and integrated startup regression cases. A real local model load, inference,
Gateway restart, second inference, and unload passed through the owner API. Restarting
Gateway preserved the independent controller process and model lease. Conversation
counts and the saved usage-limit continuation were unchanged. Site service units,
rollback source and full receipts belong in the deployment's external operations folder.
