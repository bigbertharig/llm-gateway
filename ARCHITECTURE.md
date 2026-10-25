# Architecture

`llm-gateway` is a protocol gateway, not a scheduler. It translates "a client wants model X"
into rig mode-selector calls and then proxies bytes.

## Ownership

The gateway owns:
- the offer: which models are published remotely, under which registry profile (`OFFER` in
  `tools/generate_profiles.py`), written to `remote_profiles.json`. It names no cards: the manager places each
  published profile with its fit
- its copy of the model library, `model_catalog.json` (how each model loads, the roles, the dashboard's model
  summary), which the manager and dashboard read
- its history and lessons, `lessons/` (every load the manager placed; see `lessons/README.md`)
- the OpenAI-compatible endpoint and model list (from `remote_profiles.json`)
- mapping `gpu-rig/<id>` to a profile, rejecting anything unpublished
- turning a request for an unloaded profile into one load through the manager (single-flight)
- translating rig exit codes into HTTP errors

## Boundaries with the other repos

Each of the four repos (llm-gateway, llm_orchestration, benchmarking, llm-harness) runs on its own. This one
never reads another repo at runtime:

- **From benchmarking:** the model registry (benchmarking repo `model_registry.json`) is where benchmarking
  keeps what it learns about how each model loads. `tools/generate_profiles.py` is the gateway's importer: it
  reads the library and writes `model_catalog.json` and `remote_profiles.json`, both committed here. Re-run it
  to take in new lessons; without a benchmarking checkout the gateway keeps running on its last import.
- **To benchmarking:** `lessons/` is the gateway's lessons file. Benchmarking reads it when it wants to learn from
  real loads; the gateway never writes into benchmarking.
- **With the rig:** only through `rig` commands over ssh (the rig contract below) and `rig observe`.

Still crossing a line (to fix): the dashboard imports llm_orchestration's `scripts/dashboard` package and reads
its config and the benchmark campaign logs on the shared drive (`dashboard/sources.py`), and `rig.py`'s own
published-profile path (`rig start remote <id>` without a spec) reads `remote_profiles.json` from this repo; the
manager no longer uses that path.

The rig mode selector (`llm_orchestration/shared/scripts/rig/rig.py`) owns:
- modes (idle / orchestration / bench / remote) and the busy/--force rules
- port assignment, container lifecycle; the cards come from the manager's plan
- state (`/mnt/shared/brain/rig_mode.json`)

## Rig contract

```text
rig status --json            -> {"mode", "status", "profiles": [{"id","port","state",...}], ...}
rig start remote <id>        -> exit 0 ready | 1 failed | 2 busy (other mode) | 3 locked | 4 no capacity
```

The gateway asks `status` first. If the profile isn't `ready`, it runs `start` (which also waits
for a profile someone else is already loading), retrying on exit 3, then asks `status` again for
the port.

## Placement today and later

- Today: the gateway runs on the Pi (`pi-gateway.service`), the one front door for the rig.
  `RIG_COMMAND` is a multiplexed ssh to 10.0.0.3 running `/usr/local/bin/rig`, and
  `pi-rig-tunnel.service` forwards the rig's remote ports (11434-11441) to Pi loopback, so the
  rig's runtimes stay loopback-only. Clients work on the Pi (or forward its 8080).
- Over ssh: exit 255 means the rig is unreachable (502). A `rig start` is never killed, since
  killing the ssh client can stop a load mid-way and free the rig's model-load lock early.
- LAN or external clients: put keys in `GATEWAY_API_KEYS_FILE` and bind a LAN address; the gateway
  refuses a non-loopback bind without keys.
- Later: multi-rig means one gateway route per rig prefix.

## Deliberately not here (yet)

Cloud routing (Roo does OpenRouter natively), idle auto-unload,
auto-evict when a profile doesn't fit (it returns 503 instead).
