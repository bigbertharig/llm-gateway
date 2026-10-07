# Harness work (parked)

Agent harness layer for llm_orchestration: a leader model that delegates to follower lanes on
the rig (the local version of cloud sub-agents).

**Status (2026-10-06): parked.** Current focus is making the rig a good backend for an existing
harness (Roo Code in VS Code), not building our own. "Where it runs" stays deterministic code
(rig + gateway). "What to do" belongs to the harness.

## Research (read in this order)
- `research/harness_research_summary_2026-09-30.md`: recommendation (DeepSeek Harness fork-and-own
  as backbone, Codex as cloud escalation + remote-attach reference, MCP escape hatch).
- `research/deepseek_harness_research_2026-08-14.md`: what Harness does vs what llm_orchestration
  owns. Its compaction section is the reference for long chats.

## Upstream code (kept alongside, not in this repo)
- `~/deepseek-harness`: clone of deepseek-ai/deepseek-harness at 47f943859b (2026-08-13). Not a fork
  yet, and older than the 2026-09-29 commit the summary reviewed.
- openai/codex: not cloned. Reviewed from GitHub only.

## How the 2026-10-06 work maps onto the research
| Research term | Now |
|---|---|
| roster | `remote_profiles` in `model_task_library.json` (single source; summary suggested scoreboard + Best-Of) |
| dispatcher/gateway at 10.0.0.2:8080 | this repo's gateway, running on the rig at 127.0.0.1:8080 (can move to the Pi later) |
| operator picks a profile | `rig start remote <id>`, or the gateway loads it on first request |
| report which lanes are alive | `GET /v1/rig/status` (per-profile state). Queue depth not yet |
| accurate contextWindow metadata | `/v1/models` `context_length` from the profile |
| overflow recovery needs a recognized error | gateway maps llama's `exceed_context_size_error` to OpenAI `context_length_exceeded` |
| exact token metering | gateway `/tokenize` routed by model |

## Backlog for when harness work resumes
1. Long chats on our hardware: llama-server caches the previous prompt, so only new tokens are
   processed. Compaction rewrites the history and costs a full re-process. Compact rarely and in
   big chunks (DeepSeek defaults: threshold 0.8, retain 0.16). Measure prompt tok/s per profile first
   (`tools/model_check.py`).
2. `--parallel 1` per profile: two clients alternating on one profile evict each other's prompt cache.
3. Gateway into dispatcher: per-profile queue, queue depth in status, cancel on disconnect.
4. Summary section 8 next steps (fork + prune DeepSeek Harness, bench-agent runs, roster generator).
5. Summary section 2 small fix still open: worker.py `max_tokens = worker_num_ctx` (8192).
