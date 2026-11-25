# Architecture

Gateway owns dashboard conversations, authenticated remote access, cloud accounts and
connectors, operator approvals, connection configuration and user-work coordination.
The [cross-repo contract](https://github.com/bigbertharig/benchmarking/blob/master/CROSS_REPO_CONTRACT.md) owns the shared responsibility rules; the
[cleanup plan](https://github.com/bigbertharig/benchmarking/blob/master/REPOSITORY_CLEANUP_PLAN.md) gives the implementation sequence.

## Accepted ownership

Gateway assigns requested work and supplies effective grants. Harness owns the model/tool
loop, reusable tools, bounded execution and run journals/receipts. Orchestration owns
executable local profiles, physical fit/admission, reservations, runtime lifecycle and
observed hardware state. Benchmarking owns evaluation tasks, comparative experiments
and published evidence. Each product keeps its own correctness tests.

A tool wrapper lives in Harness; the controlled operation stays with its service.
Gateway owns conversation/account operations and their authorization, while Harness
validates and invokes them through public interfaces. Gateway stores conversation history;
Harness stores execution journals. Neither writes the other's private store.

## Active deployment

- Gateway runs on the Pi with SSH/loopback access to rig services. LAN and mobile access
  have different authentication and tool grants; see [remote access](docs/REMOTE_ACCESS_SECURITY.md).
- Manager imports a hash-verified Benchmarking snapshot, selects model offers, estimates
  some context fit and coordinates loads. Orchestration selects physical cards, checks
  live admission and owns reservations. Remaining hardware-fit logic must move there.
- The offered model list can include cold profiles. The ready roster describes observed
  served identity, context and port. Ready-only callers refuse cold routes.
- Dashboard, cloud and conversation routes are mounted only when Manager is enabled.
  Several tool providers are launched from configured Harness script paths. These are
  open standalone gaps, recorded in [portability](docs/PORTABILITY.md).
- Shared MCP tools and the project-owned local-child loop are active. A generic Harness
  run/engine service and full substitution conformance are Planned.
- The legacy Markdown brain/worker system, shared task queues and private batch API were
  removed. Orchestration's remaining action queue handles hardware lifecycle operations.

## Planned interfaces and cleanup

Gateway consumes Harness's versioned run/event/receipt interface, supplies inference as
one configurable provider, and uses Harness MCP tools independently of engine choice.
The [Harness engine contract](https://github.com/bigbertharig/llm-harness/blob/main/docs/ENGINE_ADAPTERS.md)
owns run semantics. New endpoint shapes and transport/authentication are not deployed by
this document. Account credentials remain with Gateway's connector; models receive
bounded results. Standalone Harness callers supply their own provider and permission authority.

Cloud-only startup must work without Rig Manager or any sibling checkout. Missing optional
hardware or tool integrations must disable only those capabilities. Gateway's networking
role covers ingress, authentication, routing and configured connections; it does not make
Gateway a mandatory relay for standalone clients.

The reliability work includes served-model identity resolution, boot-qualified restored-run
reconciliation, idempotent load submissions, approval bound to credentialed-write arguments,
reviewed tool-schema digests and a tested-revision/artifact compatibility record. These remain
Planned until their owner publishes implementation and acceptance evidence. Candidate local
children, shared-model trials and alternative runtimes retain their own Experimental labels.
