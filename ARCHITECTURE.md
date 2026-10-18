# Architecture

`llm-gateway` is a protocol gateway, not a scheduler. It translates "a client wants model X"
into rig mode-selector calls and then proxies bytes.

## Ownership

The gateway owns:
- the offer: which models are published remotely and on which GPU shape (`OFFER` in
  `tools/generate_profiles.py`), resolved against the model registry into `remote_profiles.json`
- the OpenAI-compatible endpoint and model list (from `remote_profiles.json`)
- mapping `gpu-rig/<id>` to a profile, rejecting anything unpublished
- turning a request for an unloaded profile into one `rig start remote <id>` (single-flight)
- translating rig exit codes into HTTP errors

The model registry (benchmarking repo `model_registry.json`) owns how each model loads (GGUF,
context, batch, extra args per named profile). Benchmarking lessons land there, and re-running
the generator carries them into the offer.

The rig mode selector (`llm_orchestration/shared/scripts/rig/rig.py`) owns:
- modes (idle / orchestration / bench / remote) and the busy/--force rules
- GPU placement from each profile's `gpus_needed`, port assignment, container lifecycle
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
