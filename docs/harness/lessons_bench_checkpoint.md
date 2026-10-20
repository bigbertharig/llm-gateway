# Harness lessons from bench-checkpoint runs

Source: the bench-checkpoint prototype in the benchmarking repo
(`docker/bench-checkpoint/run_case.py`, design in `CHECKPOINT_SUITE_DESIGN.md`).
Each entry says what was observed and what our own harness should do about it.
Started 2026-10-09; add to it as more cases run.

## 1. The checkpoint was model + big context + compaction, not just the model

Observed (113 checkpoint-era Codex sessions, gpt-5.4 / gpt-5.3-codex):
context window 258,400 in every session; median session peaked at 233,761
input tokens per turn and ran at a median of 137,329; only 6 of 113 sessions
would fit in 65k; 83 of 113 hit automatic compaction.

Harness requirement: target 128k-262k contexts locally (Qwen3.6-27B already ran
at 262k on 3090 + 2x1060, 25.4 GB total incl. 8.7 GB q8 KV), and treat
compaction as a core feature, not an edge case.

## 2. Focused requests need little context; long conversations are what fill it

Observed (case CKPT-SEC-001, a small security fix with an explicit request):
peak context was 6.2k (gpt-5.4), 21.2k (Opus 4.6), 40.7k (Qwen3.8-27B), versus
a 137k median turn in the original sessions.

Harness requirement: prefer fresh-context subtasks with state on disk (the
Ralph pattern) over one long chat. Hand each subtask a precise request and a
starting file; keep the long conversation in the leader, not the workers.

## 3. Weaker models explore more before editing

Observed on the same case: gpt-5.4 finished in 9 turns, Opus 4.6 in 16,
Qwen3.8-27B used 26+ turns (mostly search and read_file) and roughly twice
Opus's context before its first edit.

Harness requirement: budget turns per task; give small models a repo map or
context hint up front; make search results precise (file:line, capped) so
exploration converges. Measure turns-to-first-edit as a harness metric.

## 4. Prompt caching decides cost (cloud) and latency (local)

Observed: Opus 4.6 cost $1.00 vs gpt-5.4 $0.05 on the same case, mostly because
the prototype does not send Anthropic cache_control, so every turn re-bills the
whole history. Locally, llama-server reuses the previous prompt prefix; any
change early in the prompt forces a full re-read (~340 tok/s on chat-27b).

Harness requirement: keep the prefix stable (fixed system prompt and tool list,
append-only history), mark cache breakpoints for providers that need them, and
log cached vs new tokens per turn.

## 5. Provider/network failures are not model failures

Observed: the first Qwen3.8-27B run ended at turn 26 on a dropped connection
(WinError 10054) from the OpenRouter provider, mid-task.

Harness requirement: retry transient errors (connection resets, 5xx, 429) with
backoff, record them separately from model outcomes, and make runs resumable
so a dropped connection does not throw away 20 turns of work.

## 6. Time-to-first-token is spiky on shared providers

Observed: Qwen3.8-27B on OpenRouter mostly answered in 1-2s, but some turns
waited 12-24s for the first token; gpt-5.4 and Opus stayed at 1-3s.

Harness requirement: per-turn timeouts that tolerate queueing, and timing
recorded per turn so local and cloud speed can be compared fairly.

## 7. Bounded tool output keeps context under control

Observed: read_file capped at 400 lines and tool output at 6,000 chars kept
all three runs well under their context limits.

Harness requirement: cap and page every tool's output by default; never dump
whole files or full test logs into the history.

## 8. Models write their own tests

Observed: all three models added a new test file next to their fix.

Harness requirement: allow it, track it (edits under tests/ are separate from
the fix), and grade with hidden tests the model never saw.

## 9. Request wording sets difficulty

Observed: the rebuilt request for CKPT-SEC-001 names the leaking field and the
area of the code; the original session discovered the leak while doing broader
QA. The case therefore tests the fix more than the discovery.

Harness / suite requirement: tag each case with how much the request reveals
(symptom only, pointed, prescriptive) and include symptom-only cases, since
that is where the checkpoint models actually earned their keep.

## 10. Import work overflows context through reading, not through output

Observed (Codex logs, 2026-10-09; window 258,400 in every session):

| Group | Sessions | Compacted | Median compactions | Model calls (median) | Tool output per session |
| --- | --- | --- | --- | --- | --- |
| checkpoint era, import | 57 | 54 | 5 (max 39) | 596 | ~1.33M tokens |
| checkpoint era, other | 56 | 29 | 1 | 146 | ~297k tokens |
| last 30 days, import | 13 | 8 | 1 (max 20) | 25 | ~511k tokens |
| last 30 days, other | 284 | 72 | 0 | 19 | ~191k tokens |

In every group about half of all tool output is file reads, and in the
checkpoint era another ~28% is search output. An import session pulls about
five context windows of tool output, so a bigger window alone does not fix it.
Fixed costs are large too: data_pipeline.md is ~53k tokens, GEOMETRY.md ~36k,
and the larger converters are 800-3000 lines.

Harness requirement for import work:
- summarize data with scripts (columns, types, null counts, a few sample rows)
  instead of reading raw files into context;
- load the pipeline contract by section, not the whole doc;
- split an import into fresh-context subtasks (profile source, write
  converter, run converter + QA, write metadata) with state on disk;
- cap reads and search output by default (lesson 7).

## 11. Quiet success, located failure

Proposed (user, 2026-10-09), following lesson 10: scans, validators, tests,
and searches should not dump full results into context. A passing check
returns one line ("all good: 38,716 rows, 197 loc_ids, 76 metrics"); a failing
check returns the failure count plus pointers to read ("3 failures; first at
converter.py:212, rows 40-55 of report.csv"), and the full output goes to a
file the model can open on demand.

Harness requirement: wrap the repo validators (ingest_handoff.py,
generate_metadata.py, run_deterministic_url_suite.py, pytest) in a summarizer
that emits status + counts + first-N failure locations, with the full log
saved to disk. This matches the existing gpu_rig work-unit pattern in
county-map-private/tools/gpu_rig/LOCAL_MODEL_PIPELINE_DECOMPOSITION.md
("read small context -> write artifact -> run deterministic command").
