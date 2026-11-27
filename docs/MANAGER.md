# Local runtime requests

Orchestration owns the Manager, including profile selection, fit, placement, admission,
leases, loading, recovery and controller heartbeats. Gateway forwards operator requests
through `orchestration-control/1` and displays the returned state.

Configure `ORCHESTRATION_URL` and `ORCHESTRATION_CONTROL_TOKEN`. `/v1/manager/...` remains
Gateway's authenticated facade; `X-Request-ID` preserves the caller's mutation identity.
The facade preserves owner response statuses and typed refusals. A model-load request
stays open through its declared card wait plus the normal load allowance; a forever wait
has no read deadline. A transport failure is uncertain and must be reconciled by its
original request ID before submitting new work.

See [Gateway standalone setup](STANDALONE.md) and the
[owner's control-plane guide](https://github.com/bigbertharig/llm_orchestration/blob/main/docs/CONTROL_PLANE.md).
`tools/rigctl.py` is an optional request client for this facade. Orchestration's `rig-control`
CLI and hardware `rig` CLI work without Gateway.
