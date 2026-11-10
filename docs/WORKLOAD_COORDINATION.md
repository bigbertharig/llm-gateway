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
- **Benchmarking:** keep the existing run IDs, claims, checkpoints, and
  cancel-by-owner path. A benchmark can share the rig with chat on disjoint
  cards once all of its launchers follow leases.
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
