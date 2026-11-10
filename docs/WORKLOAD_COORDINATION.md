# Gateway workload coordination

Status: migration contract, 2026-11-09. The current rig still has global `idle`,
`remote`, `bench`, and `orchestration` mode checks. Do not remove those checks until
every GPU launcher in orchestration and benchmarking uses the manager's leases.

## Ownership

The Pi gateway is the coordinator. It accepts operator, orchestration, and harness
work requests; chooses placements; owns the queue, priority, and cancellation
policy; records the owner of each allocation; and sends typed actions to the rig.
The rig is the physical authority. It keeps the lease, claim, and plan record,
checks GPU and port occupancy at launch, and refuses any conflicting action even
if the gateway requested it. The rig continues already accepted work if the Pi
disconnects. Boot remains idle with no GPU workload.

## Two kinds of request

| Kind | Meaning | Result |
|---|---|---|
| Suggestion | Ask the manager for a placement or start without displacing work. | Admit on free cards, offer a different placement, wait under a bounded reservation, or return an occupied/capacity reason naming the holder. |
| Command | Explicitly stop or cancel a named owner, run, profile, plan item, or lease; a card-wide kill is an emergency override. | Record what stopped and what remains occupied. A subsequent start is a separate operation and must pass the rig's normal checks. |

A command does not bypass occupancy. A forced load is currently a compound
operator command (`kill` the named cards, then fit and load); it should become an
explicit stop followed by an ordinary suggestion once callers migrate. Until
then it requires named GPUs and validates the requested model before stopping
anything. Never make a capacity preview destructive, or infer permission to
stop work from a request's priority alone.

Every request needs an owner and correlation ID. Every accepted launch needs a
lease with GPUs and port, a plan item, and a result tied to the same owner. An
occupied response should identify the holding lease, claim, profile, or run and
whether waiting or an alternate placement is possible. Retrying after a stop
must re-observe the rig; the earlier preview is not a reservation. The rig must
keep its local load lock, guard, and occupancy checks after global modes go away.

## Clients

- **Dashboard:** show the manager's offer and exact conflict. An operator can
  choose to wait, use another placement, stop a named workload, or issue the
  emergency card kill. Show the stop result before retrying a load.
- **Orchestration:** submit brain and worker needs as owned manager work. Replace
  direct GPU launches and the mode-gated watchdog with lease-aware launches and
  owner-scoped recovery. A stuck worker's stop request targets its owner or
  runtime; other cards keep running.
- **Benchmarking:** take one persistent claim over every card planned for
  the session before its first stage, including cards that are temporarily
  idle. Give the claim a run owner and purpose. Each new GPU launch takes a
  short lease naming that claim; stage completion may release the lease while
  the claim stays in place. A frozen native runtime stays occupied and claimed;
  a matching follow-up can reuse it under the same claim without launching
  another copy. Release the claim only after the session and its live runtimes
  end. Keep run IDs, checkpoints, and cancel-by-owner. A benchmark can share
  the rig with chat on disjoint cards once all launchers follow leases.
- **Harness:** request a capability and resource envelope from the gateway.
  The manager decides reuse, queue, or a new model load. The harness owns the
  agent's session, tools, context, and cancellation; it does not choose cards
  or send rig kill commands. Its inference calls use ready routes.

## Migration gates

1. Inventory every direct launcher and watchdog action in orchestration and
   benchmarking. Route each through manager admission, lease, and owner IDs.
2. Add a read-only capacity offer with typed occupied reasons, then separate
   explicit stop/cancel commands from load requests. Make command results and
   retries visible in the dashboard and durable in the rig report.
3. Prove two disjoint workloads (chat plus benchmark, then chat plus
   orchestration) continue together, and overlapping requests are refused.
   Exercise a gateway disconnect, rig reboot, stale lease, stuck worker, and
   guard stop. Verify an owner stop leaves unrelated workloads running.
4. Remove the rig-wide mode exclusion and mode-dependent watchdog behavior.
   Keep idle boot, local leases, plan recovery, GPU/port checks, and guard.

The current APIs already supply parts of this contract: `/models/fit` is a
non-mutating placement check; `/models/load` is an admission request;
`/owners/{who}/cancel`, `/profiles/{id}/unload`, `/runs/{id}/stop`, and `/kill`
are commands. They need a shared typed offer/result shape before the dashboard
and harness can present one consistent workflow.

## No-GPU verification, 2026-11-09

The Pi manager suite passed 36 tests with a fake rig and mock model server.
New cases cover all cards occupied (both fit preview and load return 503 with
no lease, plan item, or launch), owner-scoped stop followed by a fresh fit and
load while unrelated work remains, and an expired foreign lease staying
occupied until its owner releases it. Existing cases cover the stale rig
report, reboot hold/recovery, and a failed load releasing its lease. The
dashboard, chat, LAN, and gateway tests passed 50 tests using fake services.
A separate bounded Codex app-server turn returned `READY`, deleted its test
thread, and left the gateway worktree status unchanged. None of these tests
sent a load or kill to the live rig. The live gateway process was left running
while speed tests occupied the GPUs; the 503 response change takes effect on
its next restart.


## Multi-card benchmark reservation verification, 2026-11-10

The Pi Manager now reports an idle claimed card as `claimed` rather than
`free`, with its claim owner and purpose in status and placement conflicts.
The benchmark sequence can create or extend one persistent claim for all
planned cards, including cards used only in later stages. It passes that claim
to new-load leases and preserves it when a successful stage freezes a model.
An already loaded matching service can be reused under the claim without
taking a second lease. The Qwen3.8 Q4 Strata service remained healthy on GPU0
through the gateway restart; its `qwen38-q4-frozen` claim was extended to
GPUs 0?5. Live Manager status showed GPU0 occupied and claimed and GPUs 1?5
idle but claimed. The gateway Manager tests passed 88 cases, including claim
visibility and owner-checked extension. Legacy experimental scripts that
stop their own runtime still need conversion before they can use frozen
stages.
