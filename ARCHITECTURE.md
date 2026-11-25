# Gateway architecture

Gateway authenticates users, owns cloud accounts and saved conversations, presents the
dashboard, and makes requests to configured services. Orchestration owns all local-runtime
decisions. Harness owns its agent engine and reusable model-facing tools.

For local models, `orchestration_client.py` sends authenticated `orchestration-control/1`
envelopes. Orchestration returns offers, observed readiness, admission results and refusals.
Gateway keeps an in-memory display projection. It forwards inference to an owner-approved
port through the configured network path. A display snapshot is never permission to load.

The owner service runs its own reconciliation loop and controller heartbeat. Gateway
shutdown does not stop that service. There are no private cross-repository imports or
shared mutable databases. Gateway cannot execute a rig command as a fallback.

Conversation activity and operator confirmations stay in Gateway. Shared-model placement,
model resets and unload identity checks are repeated by Orchestration. Unknown versions,
response identity mismatches and missing capabilities fail explicitly. Timed-out writes
carry the original request ID; neither side silently repeats an uncertain operation.

Optional Harness tools are installed packages or configured MCP services. The dispatcher
checks imported reviewed schema digests before exposing tools. Native external coding
engines retain their own session and MCP checks. Cloud provider credentials and conversation
history remain Gateway-owned; the runtime control token is protected connection configuration.
