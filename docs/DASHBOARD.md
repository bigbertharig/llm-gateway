# Dashboard

Gateway serves the dashboard at `/dashboard/`. Its Rig view reads Gateway Manager status backed by live rig observation. The Models and Benchmarks views read Gateway's imported Benchmarking snapshot. Chat and Coding manage saved conversations, model choice, tools, and workspaces. The retired brain/worker batch tab and private Orchestration socket are gone.

| View | Current source | Main use |
|---|---|---|
| Rig | Gateway Manager and `rig observe` | Hardware health, GPU use, loaded services, reservations, jobs, and operator actions. |
| Models | Imported model catalog | Reviewed model offers, capability and placement evidence. |
| Benchmarks | Imported model evidence | Published scores, speed, and context measurements. |
| Chat | Gateway conversation and Harness tools | Research, files, local or cloud model conversations. |
| Coding | Gateway conversation and Harness tools | Workspace-bound reading, editing, commands, and agents. |

Gateway remains the controller whether it runs on the GPU host or another machine. Orchestration owns physical admission and reports service readiness or a specific refusal. The dashboard does not read shared task queues or call Orchestration's old batch API. The mobile dashboard is a separate, restricted Gateway view and reads the same Rig status.

The Upload tab sends documents through the authenticated LAN or mobile HTTPS route into the Gateway-owned `/home/bryan/Desktop/Transfer` inbox (`DASHBOARD_TRANSFER_ROOT` may override it). Gateway accepts only bounded PDF and UTF-8 text/code types, rejects client paths and unsafe names, assigns an opaque ID and generated server filename, and records size, canonical type, SHA-256, and creation time. The UI shows “File located here” with a copy-path action plus authenticated download and desktop deletion. Chats attach an admitted transfer ID rather than resending arbitrary bytes. Harness still produces a bounded text excerpt for initial model context. For Codex conversations, Gateway also copies the validated original into a private generated directory inside that conversation's authorized workspace and supplies its path, size, and digest to Codex. Git workspaces use `.git/llm-gateway-attachments/<conversation>/<upload>/`; non-Git research workspaces use `.llm-gateway-attachments/<conversation>/<upload>/`. Deleting an inbox file does not invalidate an existing conversation copy; deleting the conversation removes its scoped copy. Local-model and Claude attachments remain excerpt-only.

Persisted timestamps are UTC. Browsers convert them and successful-refresh clocks to their local timezone; the desktop clock includes the timezone abbreviation so DST and differing operator locations remain explicit.

## Code

- `src/llm_gateway/dashboard/routes.py`: dashboard API and manager actions.
- `src/llm_gateway/dashboard/sources.py`: local imported model and benchmark summaries.
- `src/llm_gateway/dashboard/static/`: desktop and mobile presentation.
- `src/llm_gateway/mobile_dashboard.py`: restricted mobile forwarding and permissions.
- `tests/test_dashboard.py`, `tests/test_mobile_dashboard.py`: focused route and presentation contracts.

See [Manager](MANAGER.md) for the admission and loading contract and [local access](LOCAL_DASHBOARD_ACCESS.md) for the LAN front door.
