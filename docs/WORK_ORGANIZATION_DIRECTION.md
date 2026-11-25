# Work organization: status and pickup notes

> Runtime ownership updated November 25: Orchestration owns local model decisions and its controller. Gateway is a request client. Earlier controller-design discussion below is historical; current setup and migration are in [local runtime requests](MANAGER.md).

Updated 2026-11-24. This guide owns Gateway's future user-work coordination design.
The [product boundary](../README.md#product-boundary) defines Gateway responsibilities;
the [implementation status](../README.md#implementation-status) tracks Gateway implementation work.
The legacy Markdown plan system is Retired. A general Gateway work graph remains Planned.

## Decision and repo ownership

Gateway owns dashboard/user-work coordination, connections, approvals and model assignment.
It consumes tool/agent execution and runtime lifecycle through their supported interfaces.
Physical admission and reservations remain with the runtime service; bounded model/tool
interaction remains with the execution engine. Comparative measurements remain with their
evaluation owner. Gateway keeps its own correctness tests and conversation records.

Gateway work-level retries, engine model/tool retries and runtime recovery have separate
attempt identities. Gateway must reconcile a possibly completed operation before repeating
it. Standalone cloud acceptance requires the other product services and checkouts to be absent.

## Present state

- Gateway dashboard Chat and Coding hold user and agent conversations. The old Markdown plan form and batch controls are removed. Gateway's manager still has some fit and loading calculations that should move behind rig admission.
- Orchestration now contains the hardware and model-runtime layer. Its rig-local action queue handles loads, unloads, and recovery; it is not a user-work planner. The brain/worker source, shared task queue commands, CPU-worker packaging, and private batch API have been removed.
- Benchmarking exports the canonical model library and validation evidence; the other repositories import local projections. Deterministic benchmark campaigns are separate from creative model onboarding and load experiments.
- Harness provides shared MCP tools and model-use behavior. Six external harnesses are references for selected features, not candidate replacements for the owned Harness.
- The GitHub analyzer and research assistant had historical plan runs. Their useful components can become reusable tool suites when Gateway work organization resumes. The `rig_search` MCP already provides a live search tool path.

## Fabro research to retain

Useful design ideas include a thin project coordinator above resource
management, reusable specialist capabilities ("shoulders"), durable project
state separate from an LLM session, authoritative receipts, human gates,
parallel independent work, and bounded repair loops. Domain applications can
keep their own source of truth and validation rules. No particular domain
project is required as the first pilot for this rig's future work system.

[Fabro's workflow documentation](https://docs.fabro.sh/core-concepts/workflows)
describes version-controlled directed graphs with agent, command, human gate,
condition, and parallel stages. Edges can loop through implement/verify/fix;
`max_visits` limits loops and goal gates make verification part of completion.
Its [project README](https://github.com/fabro-sh/fabro) shows model routing by
stage through a stylesheet, as well as durable events, checkpoints, and run
inspection. These are design references, not a decision to adopt Fabro or its
DOT format. Fabro emphasizes coding workflows; this rig also needs chat,
research, tool use, and hardware-constrained local models.

Ideas worth carrying forward:

1. Represent work as steps with dependencies, conditions, bounded retries,
   parallel branches, and explicit completion evidence. A plan is a list or
   graph of work to execute as well as possible; scarce capacity makes a step
   wait or choose a fallback without invalidating the whole plan.
2. Keep model choice separate from the work definition. Gateway manager reads
   the steps and published benchmark roles/recipes, checks live availability,
   assigns the best feasible model for each step, and sends concrete execution
   instructions to the rig and harness.
3. Preserve run events, step inputs/outputs, selected model and recipe, tool
   calls, retries, validation outcomes, and costs/timings so work can resume
   and later benchmarking can compare choices.
4. Make deterministic checks and optional human decisions explicit stages when
   the task calls for them. The harness enforces tool/model interaction rules;
   gateway owns work-level transitions.

### Scope decisions for a future implementation

- Separate project coordination from resource management as logical
  responsibilities, even when both are exposed through llm-gateway; persist
  project/run state outside model sessions; consume the owning project's
  validation receipts instead of inventing a second source of truth.
- Put new work graphs and model assignment in the gateway. The orchestration
  repo is the hardware-facing executor; its old plan brain and worker queue are removed. New plans belong in Gateway.
- Allow shoulders as capability providers with thin adapters, while also
  supporting chat, research, tool sessions, and direct work. A job need not
  be packaged as a shoulder.
- Treat shoulders and arms as reusable tool suites with typed inputs, bounded
  steps, and evidence. They can later supply steps to gateway-owned work plans;
  they are not a second scheduler or a fixed model persona. For a GitHub
  analyzer, CPU workers can resolve a URL, pin a commit, collect metadata and
  selected source files, then pass those receipts to the cheapest model that
  can make the remaining judgments. File fetches should not consume model
  tokens merely to execute predictable requests. This does not reactivate the
  legacy Markdown plan system.
- Keep the near-term rig focus on generic model capability and performance
  across rigs. When work organization resumes, pick a small job with saved
  local-model baselines; domain-specific projects retain their own rules.
- Borrow Fabro ideas without installing it or adopting its graph syntax by
  default. No new coordinator platform is approved by these notes.

## Future interface sketch

`request -> gateway work graph -> benchmark role/recipe selection -> live fit and
admission -> concrete rig execution + harness tool session -> step evidence ->
gateway next step`

The work graph should describe objectives and step needs, not GPU indexes or
specific cards. A selected execution record should name the model artifact,
runtime profile, context/concurrency target, allowed tools, fit decision, and
run ID. The hardware layer may reject or defer an actual load; gateway can then
retry, select a documented fallback, or report that step as blocked. VRAM is
the first capacity input. Artifact/runtime compatibility, adequate RAM/storage
for the chosen tiering mode, and KV/context/concurrency fit are also hard
execution constraints. Bus bandwidth and storage latency affect performance.

## When this work resumes

1. Follow Gateway's product boundary: move physical fit and
   admission decisions into Orchestration while Gateway retains operator work policy.
   A future work graph does not block standalone product cleanup.
2. Define a small versioned gateway work/step/execution contract. Include
   stable step IDs, dependencies, desired capability, selected model and recipe,
   context, allowed tools, status, and durable evidence. Keep a compatibility
   adapter for old plans only if a real use case needs it.
3. Promote one validated benchmarking recipe into a gateway-consumable offer;
   test model identity, fit, context, speed, and concurrent sessions on at least
   one rig. Preserve the benchmark receipt behind the offer.
4. Extend the project-owned Harness with `rig_search` for one gateway-managed
   research job. Compare it with a saved research assistant run for search yield,
   answer quality, latency, tool errors, and resource use. Adapt proven features
   from the six candidate harnesses behind the common tool/session contract.
5. Add a failure/resume test with an unavailable model and a busy rig. Confirm
   that the gateway records a waiting or blocked step and can continue when
   capacity returns.

Related local docs: gateway `docs/WORKLOAD_COORDINATION.md` and
`docs/MANAGER.md`; orchestration `shared/workspace/CONTEXT.md`;
harness `docs/HARNESS_SUITE_CATALOG.md` and
`mcp_servers/rig_search/README.md`; benchmarking `MODEL_LIBRARY_CONTRACT.md`,
`MODEL_LOADING_AND_CHAT_PLACEMENT.md`, and `RIG_LIMIT_TEST.md`.
