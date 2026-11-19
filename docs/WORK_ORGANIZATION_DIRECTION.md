# Work organization: status and pickup notes

Updated 2026-11-18. This is the shared direction for llm-gateway, llm_orchestration,
llm-harness, and benchmarking. It records a future work-organizing system; the
legacy Markdown plans are not an active workflow or near-term migration target.
It is the current pickup point for this work, based on the live repositories,
recent tests, and the repo-boundary decisions in this conversation.

## Decision and repo ownership

| Repo | Intended ownership | Current qualification |
| --- | --- | --- |
| **llm-gateway** | User-facing gateway and dashboard; work requests, steps, dependencies, model and recipe assignment, progress, retries, and run records. Its manager makes policy decisions using published benchmark evidence and live rig capacity. | The manager already handles fit, leases, loads, and requests. The dashboard still offers legacy Markdown plan submission. A new gateway job model has not been built. |
| **llm_orchestration** | Hardware-facing execution: rig inventory, capacity, placement, admission, model lifecycle, reservations, telemetry, and low-level execution of concrete instructions. | It still contains the old plan-reading brain, worker queue, CPU workers, and private plan API. These are legacy compatibility surfaces, not the intended owner of new work planning. Some manager hardware policy is currently in gateway; moving code is a separate decision. |
| **llm-harness** | Model-use layer: agent loops, tools (including `rig_search`), context and sessions, prompt/tool contracts, guardrails, retries within a model interaction, and harness comparison. It consumes a selected model route and tool permissions; it does not decide GPU placement. | Candidate harnesses and tool suites are under test. No single harness has been chosen for chat/dashboard integration. |
| **benchmarking** | Evidence: model and runtime evaluations, model roles, measured fit/speed/context/concurrency, hardware scans, and recipe validation. It publishes reviewed data that gateway imports. | The model library and roles are used now. `load_recipes.json` is evidence-linked but passive; executable recipe promotion and consumption are future work. |

The gateway is the authority for *what work to do and which available model or
recipe to try*. The hardware layer is the authority for *what can be placed and
run now*. The harness is the authority for *how one model uses tools and context*.
Benchmarking is the evidence source, not a live scheduler. These are ownership
boundaries, not a claim that all existing code already follows them.

## Present state

- Legacy plans are Markdown task graphs under `plans/`; the orchestration brain
  parses them, creates worker tasks, and coordinates them over shared storage.
  The GitHub analyzer and research assistant have historical runs, but the user
  has not used plans for months and does not plan to use them soon.
- The gateway now has an optional role-based assignment path for tagged legacy
  worker tasks. It resolves benchmark roles with a live manager fit check and
  sends task model metadata to orchestration. No live plan was migrated or run
  through this path. Legacy shell commands can still select a different model;
  metadata alone does not rewrite their arguments. Treat the path as a tested
  bridge, not a complete end-to-end workflow system.
- The research assistant's `execute_searches.py` calls its own DuckDuckGo
  helper in `scripts/lib/web_common.py`. The operational `rig_search` MCP lives
  in llm-harness. They overlap in purpose but are not integrated.
- Harness now offers bounded public GitHub metadata, directory, filename, and
  source-file reads at a pinned commit through the shared tool catalog. Chat can
  prefetch an explicit repository URL before the model answers. Coding retains
  separate local workspace Git inspection. This is a tool path, not a migrated
  GitHub analyzer plan or a validated full repository assessment.
- Benchmarking exports a model library and roles that the gateway imports.
  Its recent rig survey also writes `capability.json` with GPU UUIDs, VRAM, and
  measured RAM. These measurements help fit decisions; they are not fixed card
  requirements. Existing load recipes are not executable manager offers.

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
  repo is the hardware-facing executor; its plan brain/queue remain legacy
  compatibility. Do not migrate old plans now.
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

1. Confirm these repo boundaries against deployed code and decide whether
   gateway manager retains its current hardware-control implementation while
   orchestration remains the rig-side executor.
2. Define a small versioned gateway work/step/execution contract. Include
   stable step IDs, dependencies, desired capability, selected model and recipe,
   context, allowed tools, status, and durable evidence. Keep a compatibility
   adapter for old plans only if a real use case needs it.
3. Promote one validated benchmarking recipe into a gateway-consumable offer;
   test model identity, fit, context, speed, and concurrent sessions on at least
   one rig. Preserve the benchmark receipt behind the offer.
4. Connect a selected harness and `rig_search` to one gateway-managed research
   job. Compare it with a saved research assistant run for search yield, answer
   quality, latency, tool errors, and resource use. This can be a new workflow;
   it need not migrate the old Markdown plan.
5. Add a failure/resume test with an unavailable model and a busy rig. Confirm
   that the gateway records a waiting or blocked step and can continue when
   capacity returns.

Related local docs: gateway `docs/WORKLOAD_COORDINATION.md` and
`docs/MANAGER.md`; orchestration `shared/workspace/PLAN_FORMAT.md` (legacy);
harness `docs/HARNESS_SUITE_CATALOG.md` and
`mcp_servers/rig_search/README.md`; benchmarking `CROSS_REPO_CONTRACT.md`,
`MODEL_LOADING_AND_CHAT_PLACEMENT.md`, and `RIG_LIMIT_TEST.md`.
