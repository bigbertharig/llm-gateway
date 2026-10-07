# Harness Research Summary

Date: 2026-09-30 (written up 2026-10-06)
Scope: choosing an agent harness layer for `llm_orchestration`, using DeepSeek Harness and OpenAI Codex as source material.

Repos reviewed:
- `bigbertharig/llm_orchestration` (commit of 2026-09-27)
- `bigbertharig/benchmarking` (commit of 2026-08-22)
- `deepseek-ai/deepseek-harness` (commit of 2026-09-29, MIT)
- `openai/codex` (commit of 2026-09-30, Apache-2.0)

DeepSeek Harness and Codex were reviewed from docs, READMEs and interface code. Neither has been built or run yet.

---

## 1. Goal

Talk to one model and have it hand work out to the other models on the rig. This is the local version of cloud sub-agents.

- The **leader** is the model the user talks to. It can be the strongest model on the 3090, the stacked 70B, or a cloud frontier model.
- **Followers** are the models loaded on the worker GPUs.
- The rig keeps running when the laptop disconnects. The laptop, phone or other machine is only a client.
- Scaling means adding GPUs or rigs adds capacity. The harness uses whatever is there.

Model loading, benchmarking, leases, split runtimes and thermal handling already exist. **The new work is the harness layer.**

---

## 2. Where the current system stands

- `llm_orchestration` is a strong **scheduler and runtime control plane**: GPU leases, split-runtime state machines, thermal recovery, hot/cold loading, a file-based queue over NFS, retries and quarantine.
- It has **no agent loop**. In `worker.py`, an LLM task is one `/v1/chat/completions` call: prompt in, text out. The model never reads files, runs tests or retries on errors. That missing loop is what a harness provides.
- `benchmarking` already has most of what the roster needs:
  - The "Best By Rig Lane" table in `MODEL_BEST_OF.md`, with its "use it for" and "escalate when" columns, is effectively a list of sub-agent descriptions.
  - `results/model_library_scoreboard.json` is the machine-readable source for it.
  - `docker/bench-agent/agent_harness.py` is a small working tool-loop harness: OpenAI-compatible tool calls, schema validation, sandboxed artifacts.

### Small fixes noted
- `worker.py` sets `max_tokens = worker_num_ctx` (8192), so it requests an output as long as the whole context window. The cap should be the context size minus the prompt, or a fixed output limit.
- Model lists have drifted apart:
  - `MODEL_BEST_OF.md` (2026-08-22) says `ministral-3:14b` for split-worker code.
  - `model_task_library.json` (2026-04-28) still says `qwen2.5-coder:14b`.
  - `model_access.json` lists `qwen2.5-coder` 7b/14b and `qwen3.5:4b`.

  Pick one source (scoreboard + Best-Of) and generate or retire the others.
- `bench-agent` has **no real-model runs yet**. It measures tool choice, argument validity, error recovery and handling of changed interfaces, which is exactly what decides whether a lane can use native tool calls.

---

## 3. Architecture decisions

### 3.1 The leader doesn't need to know what model it runs on
Any harness that can delegate to sub-agents and call tools can be the leader. Build **one interface to the rig** and the leader can be cloud or local with only a config change.

### 3.2 You pick the profile; the leader works with the roster
Simpler than letting the leader reload GPUs:
1. The operator loads a profile (swarm, mixed, monolith, or custom) using the existing benchmark-backed loading.
2. The rig publishes a **roster**: model, number of lanes, context size, rough speed, and "good at" / "escalate when" notes taken from benchmark evidence.
3. The leader treats each roster entry as a **sub-agent type** and delegates the same way it would to cloud sub-agents.

Optional later: a `request_profile` tool, gated behind operator approval.

### 3.3 Separate "what" from "where"
- **What work to do** (break down the goal, write specs, judge results, escalate) belongs to the leader's model.
- **Where and when it runs** (leases, loads, splits, thermal, queues, retries on hardware failure) stays deterministic code.
- The existing brain is mostly kept as the dispatcher. Its LLM planning parts (`brain_plan`, `brain_goal`) get demoted or retired. The 3090 is freed up for the leader, or to act as the strongest follower when the leader is in the cloud.

### 3.4 Rig profiles (stacking)
| Profile | Layout | Good for |
|---|---|---|
| Swarm | 3090 = leader/strong model, each 6GB card = small follower | Many narrow tasks, best-of-N |
| Mixed | 2×6GB split → 14B, remaining cards small | Harder tasks alongside routine work |
| Monolith | All cards stacked → 70B (tested: slow but works) | Deep review, hard reasoning, fully local smart leader |

Switching profiles costs minutes, so it should be rare. Overnight pattern: generate candidates in Swarm, switch to Monolith to review and rank them, switch back.

### 3.5 Durability and remote access
- The leader harness runs **on the rig side** (Pi or rig CPU), not on the laptop. Clients attach and detach.
- Keep the outbound-only, allowlisted posture from `remote_mode.md`. The rig interface accepts task specs, never arbitrary shell commands.

### 3.6 Dispatcher = gateway
With DeepSeek's in-process `spawn` sub-agents, a follower's tools run inside the harness process and only the model calls go out to lanes. The rig-side dispatcher becomes the **gateway**: route by model name to a lane, queue requests, hold the lease. That needs no harness code.

### 3.7 Unit of exchange for code work: git patches
The leader pushes a branch. Each follower attempt runs in its own worktree. Followers return diffs plus verify output. The leader reviews and merges.

---

## 4. DeepSeek Harness findings

Released 2026-08-13, MIT, Node/TypeScript, about 300 packages built on Cordis ("everything is a plugin"). The README warns it is a developer preview with breaking changes. Outside PRs are not accepted.

**What fits our plan:**
- **Custom endpoints:** `llm-pi-ai` accepts hand-declared routes (`api: openai-completions`, `baseURL`, a `models` list with `contextWindow`/`maxTokens`). It works with llama-server today.
- **Roster as sub-agents:** mount `tool-subagent` once per lane. Each instance has its own `toolName`, fixed `agentOptions` (provider/model/maxTokens), `persona` and `toolFilter`. Alternatively, `modelSelectionSettings` plus `list_subagent_models` lets the leader choose a model per call.
- **Sub-agent backends:**
  - `spawn` (fresh child, in-process)
  - `fork` (child seeded with the parent's completed turns)
  - `dsh-sdk` (full harness in a separate process)
  - `acp` (any ACP agent)
  - `codex`
  - `claude-code`
- **Already built and relevant:**
  - `workflow`: the leader writes JS with `agent()` / `parallel()` / `pipeline()`. This is the best-of-N swarm pattern.
  - `tool-ralph`: fresh-child rounds against one fixed objective. Note that round reports are **not independently verified**, so add a verify step.
  - `goal`: an objective that persists across turns and restarts.
  - `experimental/agent-team`: lead + teammates + durable messages + shared task board.
  - `ssh/*`: filesystem, subprocess and sandbox layers over SSH, so tools can run on the rig while the harness runs elsewhere.
  - `bundle/headless`: `dsh --profile headless "task" --json`. There is also a Python SDK (JSON-RPC over stdio).
  - `session-persistence-jsonl`: append-only session log. Resume, fork, replay and UI are all built on it.
  - `guard/repeat-tool-reminder` and `compaction-tool-result-pruner`: useful protections for small models.
- **Correction to earlier press coverage:** the shipped `minimal` preset is only a persona and a persistent bash shell. It does not include `str_replace_editor`. Build our own follower preset: bash + `str_replace_editor` + repeat-tool-reminder + tool-result-pruner.
- **Telemetry packages exist** (`product-telemetry-otel`, `client-product-analytics`, `session-telemetry-otel`). Remove them in the fork rather than trusting their defaults.

---

## 5. OpenAI Codex findings

- The Codex harness has been Apache-2.0 open source for a while. DevDay (2026-09-29) presented it as the base under Dots and the Agents API. The genuinely new open-source release was the **Codex Security CLI** (`openai/codex-security`).
- **Hard blocker for local lanes:** Codex removed Chat Completions entirely (`wire_api = "chat"` errors). It only speaks the Responses API. llama-server's `/v1/responses` is still partial and non-compliant (llama.cpp issue #19138). Running Codex on rig lanes would need a Responses→Chat translation in the gateway.
- **Correction:** Codex *does* have multi-agent now: `spawn_agent`, `agent_max_threads`, `agent_max_depth`, and **agent role files** (TOML with a description plus a config layer that can set provider and model).
- **Strongest remote design available:**
  - `app-server`: JSON-RPC with threads, turns and items; streaming started/delta/completed; approvals.
  - `app-server-daemon`: a durable app-server launched over SSH that desktop and mobile clients attach to (experimental).
  - `exec-server`: run processes and filesystem operations on another machine over websocket.

---

## 6. Recommendation

- **Backbone: DeepSeek Harness, forked and owned**, for both leader and followers. It works with the llama-servers today, the roster is config rather than code, and the swarm tools already exist.
- **Codex:** use it as a cloud escalation target (via `dsh-subagent-codex`), and as the reference design for durable remote attach (`app-server-daemon`, `exec-server`, the approvals flow).
- **Keep an MCP escape hatch:** expose the rig as a plain MCP server (roster / spawn / status / result / cancel) so Claude Code or Codex can be the leader when a frontier model with its own harness is wanted.
- **The roster generator writes two things:** the pi-ai route `models` list, and one `tool-subagent` instance per lane.

Config sketch (untested):

```yaml
- name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      rig:
        api: openai-completions
        baseURL: http://10.0.0.2:8080/v1      # gateway
        models:
          - { id: ministral-3-14b, contextWindow: 32768, maxTokens: 4096 }
          - { id: qwen2.5-coder-7b, contextWindow: 16384, maxTokens: 2048 }
- name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: coder_14b
    agentOptions: { provider: rig, model: ministral-3-14b, maxTokens: 4096 }
    persona: "Split-worker coder. Good at: data prep, focused code. Escalate hard planning."
```

### Fork-and-own approach
- Fork DeepSeek Harness. Don't depend on upstream. Pull upgrades selectively when wanted.
- Put customizations in **your own packages and profile YAML**. Keep any core patches as separate, labelled commits so cherry-picking from upstream stays cheap.
- **Prune** to roughly 40 packages:
  - core, `llm` + `llm-pi-ai`, tools
  - sessions + `jsonl`
  - subagent, workflow, goal, agent-team
  - fs + `str_replace_editor`, shell/terminal, sandbox
  - compaction, guard
  - ssh, headless/sdk-minimal, acp, mcp

  Drop the desktop app, voice/speech, browser-use and computer-use, DeepSeek account/credentials, and analytics/telemetry.
- From Codex, take designs (app-server-daemon, exec-server protocol, role files) and possibly binaries (`linux-sandbox`/bwrap). Keep the Apache NOTICE and license text on anything copied.
- **Language boundary:** Python owns the rig, leases, gateway, roster and benchmarks. The forked Node harness owns the agent loop. They connect through the generated YAML, the Python SDK, headless `--json`, or ACP. Don't port the harness to Python.
- Keep an `UPSTREAM.md` recording the fork commit and anything pulled in since.

---

## 7. Lessons learned

### About harnesses in general
1. **A harness is everything between the model and the world:** the loop, the action format, context management, state, verification, permissions/sandbox, and delegation.
2. **The weaker the model, the more of the intelligence has to live in the harness.** Frontier models tolerate a sloppy harness; 4–14B models don't.
3. **Few tools for small models.** Two to four tools (bash + an editor) preserve context and leave fewer ways to fail.
4. **Search-and-replace editing is easier for small models** than patch formats like Codex's `apply_patch`. Text edit formats or GBNF-constrained output help when native tool calls are unreliable.
5. **Fresh context each iteration, with state kept on disk** (the Ralph pattern) works better than one long conversation for small models.
6. **Verification comes from outside the model.** Tests, linters, type checks and schema validators decide when the work is done.
7. **Context sizes:** tool calling commonly breaks below about 16–32k context, and small models degrade well before their advertised limit.
8. **For local swarms, use best-of-N with a verifier, not agents talking to each other.** Running in parallel is nearly free locally, but coordination is hard for weak models. Let the harness coordinate and keep each model's job narrow.
9. **Results depend mostly on how well the leader writes specs.** With a local leader, force specs into a fixed shape (scope, verify command, acceptance criteria).
10. **Supervision loop:** verify → retry with the error → move up a tier (1×card → split → 3090 → leader → cloud). The "escalate when" column already describes this ladder.

### About the harness landscape
11. **Check press coverage against the source.** Coverage said DeepSeek's minimal mode had `str_replace_editor` (its shipped preset doesn't) and called Codex single-agent (it isn't anymore).
12. **"Open-sourced yesterday" can mean repositioned.** Codex was already open source; DevDay presented it as the platform under OpenAI's new products.
13. **Which API a harness speaks matters for local models.** Codex's Responses-only stance excludes llama-server for now. Harnesses that speak Chat Completions (DeepSeek, OpenCode, Pi, Goose) work today.
14. **A plugin architecture makes fork-and-own practical.** Most customization lands in new packages, not changes to their core.

### Operational lessons that carry into the harness
15. **Check what model a port is actually serving** (`/v1/models`) before advertising a lane. Port 11434 once served the wrong model while the controller credited results to three others.
16. **Always set explicit output token limits.** An unbounded server default once pushed a smoke run to the thermal warning line.
17. **Thermal limits cap real parallelism.** Agent swarms create sustained load. The dispatcher may need to advertise fewer concurrent lanes than there are GPUs, or throttle as temperatures rise. A new CPU cooler is pending.
18. **Report which lanes are alive and how deep the queue is**, not just a static roster, so the leader doesn't queue behind a dead or wedged worker.
19. **Within a rig, stack models; across rigs, add lanes.** Splitting one model across machines over gigabit is too slow. A second rig adds follower lanes or its own monolith.

---

## 8. Next steps

1. Fork and prune DeepSeek Harness, remove telemetry, confirm the build, and add a skeleton `rig` profile (gateway route + one follower preset).
2. Run `bench-agent` on the four lane defaults, and add a "worktree coding task with tests" case family. Use the results to choose each lane's tool format (native tool calls vs. text edits vs. GBNF).
3. Build the roster generator: scoreboard + loaded profile → pi-ai route + `tool-subagent` instances, with model-identity checks.
4. Extend the gateway into the dispatcher: route by model name to a lane, queue, lease, health, queue depth.
5. Test one DeepSeek follower against one 1060 lane on a small worktree task that has tests.
6. Later: durable leader on the rig with remote attach (borrowing Codex's `app-server-daemon` design), an MCP escape hatch, and a ledger of production outcomes feeding back into the roster.

---

## Sources
- [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
- [openai/codex](https://github.com/openai/codex)
- [DeepSeek open sources an agent harness where everything is a plugin (The New Stack)](https://thenewstack.io/deepseek-harness-open-source-plugins/)
- [Introducing the Agents API (OpenAI)](https://openai.com/index/introducing-the-agents-api/)
- [Unlocking the Codex harness: how we built the App Server (OpenAI)](https://openai.com/index/unlocking-the-codex-harness/)
- [Codex as a platform (OpenAI Developers)](https://developers.openai.com/blog/codex-as-a-platform)
- [OpenAI DevDay 2026 live blog (Simon Willison)](https://simonwillison.net/2026/Sep/29/openai-devday-2026-live-blog/)
- [OpenAI DevDay 2026, everything announced (Decrypt)](https://decrypt.co/379584/openai-ai-agents-computers-devday-2026-everything-announced)
- [Best Open-Source Agent Harnesses for Local LLMs in 2026 (MarkTechPost)](https://www.marktechpost.com/2026/09/18/best-open-source-agent-harnesses-for-local-llms-in-2026/)
- [llama.cpp issue #19138: Responses API support in llama-server](https://github.com/ggml-org/llama.cpp/issues/19138)
