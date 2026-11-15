# External repositories

This is the source trail for code the gateway operates or integrates with.
The [four-repo map](README.md#the-four-repos) covers our own projects.
Last checked: 2026-11-15.

| Upstream | Role here | Current integration / source location |
|---|---|---|
| [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | `llama-server` provides the inference routes behind the gateway | Rig runtime images and build pins are owned by [llm_orchestration](https://github.com/bigbertharig/llm_orchestration); check its external repo index before an upgrade. |
| [Niko1221/Strata](https://github.com/Niko1221/Strata) | Alternative managed inference backend for registered profiles | Rig release bundle `/data/bench-staging/strata-v0.2.10` is pinned at Control/engine 0.1.56. Pi source checkout `~/Strata` was at `baf16363` on 2026-11-15; source is newer than the deployed bundle. See [benchmarking's Strata evidence](https://github.com/bigbertharig/benchmarking/blob/master/STRATA_MEMORY_TEST_PLAN.md). |
| [openai/codex](https://github.com/openai/codex) | Dashboard Codex bridge and remote agent client behavior | Integration docs: [POWERFUL_GATEWAY_AND_LOCAL_AGENTS.md](docs/POWERFUL_GATEWAY_AND_LOCAL_AGENTS.md). The reference checkout and reviewed revision are tracked by [llm-harness](https://github.com/bigbertharig/llm-harness/blob/main/UPSTREAM.md). |

## Checking for updates

The source links above are the upstream projects. A local checkout or release
pin is a snapshot, not an automatic update. For a checkout, run
`git -C PATH status --short --branch`, then `git -C PATH fetch origin` and
`git -C PATH log --oneline HEAD..origin/BRANCH` (replace PATH and BRANCH from
the table or the checkout's `git branch -r`). Review release notes and changes
before changing a deployed binary, model recipe, or benchmark baseline. Record
the new source revision and the validation receipt here when adopting it.
