# Qwen 35 documentation delegation trial, 2026-11-20

Status: observed experimental behavior, not a model-wide capability claim.

The unification report recommended using bounded local agents for basic work and independently reviewing their output. Two Qwen 35 children received read-only workspace access. Neither child had shell or file-edit authority. Raw receipts and event streams remain local under `llm-harness/results/`; this page preserves the non-sensitive acceptance evidence.

| Trial | Workspace and task | Receipt evidence | Review outcome |
|---|---|---|---|
| Contract review | Benchmarking; inspect the cross-repository contract and propose the smallest truthful status/compatibility edit | `qwen3.6:35b-a3b-xl`, 65,536-token context, seven successful text-bridge reads, 65,414 result bytes, 110.73 seconds, exit 0, reservation released, no runner error | Useful gap extraction and correct source identification. Rejected as an edit because the proposed Markdown truncated mid-sentence and one ownership inference was broader than its cited table. |
| Architecture status | Gateway; read `ARCHITECTURE.md` and `README.md`, then draft one short status subsection | Same model/context, zero tool calls, 13.87 seconds, exit 0, reservation released, receipt error `child did not execute an allowed tool` | Rejected completely. It invented a nonexistent insertion point and unsupported repository-state claims without reading either file. |

The accepted documentation changes were written and reviewed by the parent after reading the report and repository sources; no child draft was pasted verbatim.

## Trust rule from this trial

For repository documentation, accept a local-child proposal only when all of these hold:

1. The receipt has no error and shows successful reads of every source used for a factual claim.
2. The answer is complete and satisfies its requested structure; exit 0 and a stable roster do not prove content completeness.
3. Every claim about deployed behavior is independently checked against current source, tests, or a maintained contract.
4. The final diff is reviewed by the parent. Workspace-read children remain proposal-only until a separate edit policy and approval path exist.

The first trial's bridge receipt reports a 12-call, 32,000-byte-per-result, 80,000-byte-total budget. Native adapter events use different accounting today, so this trial does not establish equivalent budget enforcement across adapters.
