# Dashboard

One page for the whole setup, served by the gateway on the Pi: `http://127.0.0.1:8080/dashboard/`.
An enrolled desktop can open `https://raspberrypi.local/gpurig` directly through the
[local HTTPS front door](LOCAL_DASHBOARD_ACCESS.md). It replaced the
orchestration dashboard (port 8787) on 2026-10-20. Orchestration now serves its status and controls
through a versioned local API on a private Unix socket; the gateway does not import its code.

The main gateway listens on loopback only and has no API keys yet. The separate Wi-Fi front door
requires a browser client certificate and forwards only dashboard routes. The page and its actions
remain available to anyone with a shell on the Pi or a forwarded SSH connection; enroll only trusted
operator devices for direct access.

## Tabs

| Tab | Shows | Actions | Source |
|---|---|---|---|
| Rig | every card in board order (verdict, holder, temperature, fan, power against its limit, VRAM, load); claims drawn as brackets over the cards they reserve; loaded models; the rig plan; manager events | kill a card, unclaim, unload a model, cancel a plan item, load a model (cards, use, wait for busy cards, force) | the manager (`/v1/manager/status`, `docs/MANAGER.md`); actions are the manager's overrides, the same as `rigctl` |
| Models | every registry model: status, placement, roles, profiles, the best result per key test (a limit-50 run over a smoke run, then the newest), pipeline mean, decode speed and context from the latest runtime characterization, download link | none | benchmarking `model_registry.json`, `model_roles.json`, `results/model_benchmark_records.jsonl`, `results/runtime_experiments/` |
| Benchmarks | lane runs (running while the lane holds its `lane-<cards>` claim) and campaign runs with their block states | none (start runs as before) | `logs/benchmarks/worker_lanes_*`, `logs/benchmarks/campaigns/history/*/*/status.json` on the drive |
| Orchestration | task counts, alerts, workers, active batches | start a plan, resume, kill, show outputs, kill all active plans, clean up stale task files, requeue a worker's tasks | llm_orchestration `GET /v1/orchestration/status`, `GET /v1/orchestration/options`, and `POST /v1/orchestration/actions/{action}` over `/run/llm-orchestration/api.sock` |

The old dashboard's GPU-level controls (`clear_runtime`, `reset_gpu`, `return_default`, remote runtime
cleanup) are gone: they acted on the rig outside the manager. Kill or unload through the Rig tab, then
requeue that worker's tasks on the Orchestration tab.

## Code

| File | Owns |
|---|---|
| `src/llm_gateway/dashboard/routes.py` | the page at `/dashboard/` and its API under `/dashboard/api/` (`rig`, `rig/{kill,unclaim,unload,cancel,load}`, `models`, `benchmarks`, `orchestration`, `orchestration/options`, `orchestration/{start_plan,resume_plan,kill_plan,kill_all_active,batch_outputs,cleanup_stale,requeue_worker}`) |
| `src/llm_gateway/dashboard/sources.py` | Models and Benchmarks readers, plus the orchestration API client (the routes run them in a thread) |
| `src/llm_gateway/dashboard/static/index.html` | the page: one file, no build step, no dependencies beyond two web fonts |
| `tests/test_dashboard.py` | page, rig API and overrides, both summaries, orchestration API forwarding and unavailable socket |
| `src/llm_gateway/lan_dashboard.py`, `tests/test_lan_dashboard.py` | restricted LAN proxy, route and browser-origin checks |

Settings (environment, all optional): `ORCHESTRATION_API_SOCKET` (default
`/run/llm-orchestration/api.sock`), `SHARED_ROOT` (default the first of
`/media/bryan/shared`, `/mnt/shared`), `BENCHMARKING_ROOT` (as for model loads).
`ORCHESTRATION_CONFIG` belongs to the orchestration API process, not the gateway.

The API is accessible only to the local service user. No remote access is enabled by this migration.
The gateway keeps the same browser routes and response shapes. The two services can be restarted and
upgraded independently; `/v1/orchestration` is the versioned cross-repo contract.

## Next

- An Anthropic-compatible `/v1/messages` on the gateway, so Anthropic-API agents can use local models
  (seen in RigUp, 2026-10-20).
- New releases from the publishers in the registry's `source_url`s, for NEW_MODEL_INTEGRATION Phase 0.
- API keys for the gateway before anything other than loopback can reach it.

## Chat tab (local dashboard)

### Codex coding sessions (live 2026-11-09)

The model dropdown also offers **Codex (cloud coding agent)**. Selecting it and clicking New conversation
opens a separate in-page chat tab. The selected Pi workspace is fixed for that conversation. Codex
uses its own persisted app-server thread, so several Codex and local chats can run concurrently.
Assistant replies render a safe Markdown subset (headings, lists, quotes, code, emphasis, and links).
Web links open in a new tab; dashboard-style links to Pi-local files copy the path instead of exposing
the filesystem through HTTP. Codex conversations accept up to three pasted or selected PNG, JPEG, or
WebP images (5 MiB each, 10 MiB total). Images are shown before sending, passed to app-server as image
inputs, and saved with the transcript. Local rig chats remain text-only until a profile explicitly
advertises and implements vision input. The HTTPS proxy permits a 15 MiB request body so base64
image uploads reach the dashboard; the dashboard proxy applies that size only to chat turns and
keeps its 1 MiB limit for other POST routes.
The composer has a browser-local **Enter sends** preference. It defaults on: Enter submits and
Shift+Enter adds a line. Turn it off to make Enter add lines and submit only with the Send button.
Completed agent answers and turn-completed notices are excluded from the activity panel because the
same answer is already rendered in the saved transcript; command and file-change activity remains.
The dashboard's device-code sign-in connects the Pi service user's Codex installation to the
operator's ChatGPT account; the browser receives only the verification URL and one-time code.
Credentials remain in Codex's Pi-local auth store. The agent starts with `workspace-write` access
to its selected workspace, `networkAccess: false`, and on-request approvals. The sidebar keeps the
top-level local/Codex choice. Within a Codex chat, the composer lists the available Codex models
and each model's supported reasoning efforts from app-server. The approval selector offers
**Ask for approval** or **Approve for me**; the latter sets Codex's `approvals_reviewer` to
`auto_review` without expanding the workspace sandbox. Full access is shown as unavailable in
the gateway dashboard. These settings are saved per conversation and affect future turns.
The dashboard shows pending command or file-change approvals in the conversation and offers
one-time Approve or Decline when manual approval is needed. Other
app-server interaction types are currently rejected. The workspace allowlist comes from
`CODEX_CHAT_WORKSPACES` (colon-delimited); its default is the Pi gateway, orchestration, and
harness repos. The service user needs a current `codex` executable and file access to these repos.
The gateway starts app-server lazily and restarts it after a gateway restart; SQLite keeps the
dashboard conversation-to-Codex-thread mapping. The sidebar lists every saved conversation, and
reopening one restores its transcript and selected workspace. A Codex prompt is saved as a turn
before execution. The send request returns with a turn ID while Codex works on the Pi; the browser
polls saved turn state and agent activity, so switching tabs or closing the page does not cancel
the turn. A successful reply and completed state are committed together. Failure details are saved
with the turn and shown when the chat is reopened. Live activity and approvals share the transcript's
single scroll area; completed activity is saved beside its answer in an expandable group.
A gateway restart
marks any unfinished turn interrupted and shows its prompt when reopened. The gateway never
automatically resends an interrupted prompt because the agent may already have performed actions.
Check the workspace and service state before choosing whether to send a follow-up. This is turn
recovery visibility; a gateway process restart still interrupts an in-flight agent run.

Typing `/` in a Codex chat opens the browser command picker. `/new` opens a new Codex tab;
`/resume` lists saved Codex chats and `/resume <ID or unique title>` opens one. `/status` shows
the current thread, workspace, and saved turn state. `/compact` invokes app-server context
compaction after a completed turn, `/diff` shows the selected workspace's changed files and
tracked Git diff, and `/model` lists or selects a Codex model for future turns. `/help` lists these
supported browser commands. Other Codex CLI slash commands
are not yet mapped to the dashboard and are rejected visibly instead of sent to the agent as text.

Verification on 2026-11-11: the Pi's focused dashboard/chat tests passed 18/18. A live dashboard
Codex turn with a generated PNG returned a turn ID in 0.01 seconds, completed with a description
of the image, and the temporary conversation was deleted. Live `/model` and `/diff` command
routes returned 200. The gateway and LAN dashboard proxy were restarted; both remained active.

Verification on 2026-11-09: the desktop chat and LAN contract selection passed 5/5; its full suite
passed 147 tests with two existing Windows-only failures (Unix-domain socket support and POSIX
directory mode assertions). The browser JavaScript passed `node --check`. On the Pi, the full
gateway suite passed 149/149, Codex CLI 0.147.0 recognized the existing ChatGPT Plus sign-in,
and a bounded no-edit Codex turn returned `READY`; its test thread was deleted. The gateway,
LAN proxy, and Nginx services were restarted and active. Through the LAN proxy, the dashboard
and Codex account routes each returned 200. Browser rendering and a live approval decision have
not yet been exercised.


The Chat tab is basic saved model chat, not a harness agent session. It lists the active chat profiles marked `dashboard_chat` in the gateway's generated official profile offer; it has no hardcoded model IDs. Selecting one and clicking "New conversation" creates and opens the saved conversation immediately, then asks the manager to load that profile if cold. The Chat tab shows load progress and disables Send until the manager reports ready; a failed load leaves the empty conversation saved and offers Retry model load. Sending then uses the gateway's shared inference forwarding path for the OpenAI-style chat request. The choice of profile is explicit; no model is loaded merely by opening the Chat tab. The manager currently favors the fastest free GPU for chat, which can be the 3090 even for a small model; the UI requires an explicit model choice and tells the operator to check Rig before sending. During a turn the UI shows elapsed time and switches to generating once the ready route appears. The current basic UI sends up to 16 recent saved messages for context and limits each new message to 8,000 characters. It shows the manager-reported loaded context length at preparation and, after a reply, the model server's reported prompt, output, and total token counts for the last completed turn against that window. Counts are persisted with the conversation and cleared when its model changes. Older conversations and servers that omit usage show an unavailable measurement rather than a guessed one. This is a last-turn snapshot, not a prediction of the next request or a claim that all saved messages fit. Cold loading or generation can take several minutes.

Each conversation stores its selected profile. Opening a saved conversation restores that choice; sending a turn with another model is rejected until the operator explicitly chooses "Continue this conversation with selected model." "New conversation" starts a separate history with the chosen model and begins preparation before its first message. Reopening an existing saved conversation remains immediate; if its model was unloaded, the next Send prepares it. Historical messages retain their producing model label. Switching changes future turns only and does not reload the model until a message is sent. The SQLite transcript belongs to this basic Chat tab; the planned harness session service will own agent instructions, tools, compaction, delegation, approvals, and durable event replay when implemented. See `docs/REMOTE_SESSION_ACCESS_DESIGN.md` and the harness companion design. Do not treat the basic Chat transcript as a harness session.

### Saved transcript and runtime checkpoint

Opening a saved dashboard conversation restores its SQLite transcript and
selected profile. The next turn currently sends only its latest 16 messages
to `llama-server`; its RAM prompt cache may reuse a prefix, but the dashboard
does not own an exact, durable KV state. Thus "resume old chat" means reopen
the saved transcript, not resume a worker GPU context after a process restart.

The benchmarking repo's
`results/runtime_experiments/model_loading_sequences/qwen35-return-durable-20261109/README.md`
is a separate experimental runtime state format. Before integrating that
mechanism, bind a checkpoint to the dashboard conversation ID, model/profile
identity, KV format, and committed message/turn number. The manager would
validate and restore it; the dashboard would keep owning transcript selection
and display whether a turn used a warm context, restored checkpoint, or
history replay. A completed answer and its transcript row must agree with the
checkpoint's committed turn. The current prototype has not solved that
delivery transaction or on-disk compaction. Keep this integration behind
speed/capacity validation of the worker path.

Conversations and messages are saved in SQLite under `CHAT_DATA_ROOT` (default `~/.local/share/llm-gateway/chat`). On the Pi, `deploy/pi-gateway.service` sets this to `/media/bryan/shared/llm-gateway/chat` on the 4 TB volume and requires that mount before startup. Change that environment value to relocate history; stop the service and move the directory as a unit. The directory is mode 0700 and the database mode 0600. The dashboard provides new, list, open, send, and delete operations. No cloud sync or remote access is involved.

For now, the Chat tab displays a manager-owned "Rig busy" signal and a cold turn returns 409 before loading when another job, profile, claim, lease, runtime, or GPU load is present. The same ready chat profile can continue alone. A failed preparation or turn does not add messages to the saved conversation. This guard does not stop or replace other work, and the manager's lease checks still arbitrate any race after the signal. When idle, cold Chat loads use the manager's automatic placement. The manager plans the largest context that fits its selected fast placement; the UI does not yet present a capacity negotiation when the 3090 is busy. A future manager response should offer choices such as "use free 1060s now" versus "wait for 3090" with projected context and measured speed when available. Chat should render the manager's options without deciding placement or fallback itself. It must never silently stop existing work or switch the conversation's model. See `docs/MANAGER.md` for the proposed offer contract and tests. Loading a large context does not expand this basic UI's 16-message history limit; context use and truncation need separate work before the UI can claim full-history use.

Live verification on 2026-11-06: the LAN mTLS path served the Chat tab and four model choices; a conversation was created, read, and deleted through that path; a short turn cold-loaded `harness-gemma4-e4b-64k`, returned `chat path working`, and saved two messages before the test conversation was deleted. The manager placed that 64K-context chat profile on GPU 0 (3090), reflecting its current fastest-card policy. That test profile was unloaded afterward and the rig reported no loaded profiles and GPU 0 free. The model chooser now starts empty and warns about placement before the first send.

Live integration check on 2026-11-06 after sharing the inference forwarding path: the rig started with an empty ready roster. Explicit manager loads reached ready on GPU 1 for `harness-gemma4-e4b-64k` (26.8 s), GPUs 2 and 3 for `harness-gemma4-12b-64k` (143.2 s), and GPU 0 for `gpt-oss-20b` (44.5 s). The ready-roster GPU IDs matched each request, a Chat turn through the gateway returned `OK` for each, each test conversation was deleted, and each profile was unloaded. The final ready roster was empty. These were basic completion checks; they do not validate harness agent sessions or the browser's visual presentation.

On 2026-11-06, after enabling maximum-fit context for published Chat loads, a live cold turn with `harness-gemma4-e4b-64k` served 131,072 context on GPU 0 and answered `OK`. The manager's rig load item took 252 seconds and the whole turn 257.7 seconds; the profile was unloaded and the final ready roster was empty. The visible Chat menu now comes from `dashboard_chat` flags in the generated published offer; live options still listed the same four profiles.
