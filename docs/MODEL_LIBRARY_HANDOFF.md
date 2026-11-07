# Benchmarking to Manager model library handoff

Benchmarking owns model experiments, receipts, `model_registry.json`, and
`model_roles.json`. It validates its machine-readable library with:

```bash
cd /media/bryan/shared/plans/shoulders/benchmarking
python3 scripts/active/model_library.py export
python3 scripts/active/model_library.py check
```

On the Pi, Manager asks what changed and imports it with:

```bash
cd /home/bryan/llm-gateway
.venv/bin/python tools/sync_model_library.py --dry-run  # report only
.venv/bin/python tools/sync_model_library.py            # validate and import
.venv/bin/python tools/sync_model_library.py --check    # 0 current, 1 pending, 2 invalid
```

`BENCHMARKING_ROOT` selects another checkout. The import rejects an invalid or
unexported benchmarking library before changing Manager files. It reports
added, removed, and changed models, roles, and published offers, plus a SHA256
fingerprint of the registry, roles, and benchmarking converter. It prepares both
outputs before replacing either and restores the previous pair if a write fails.

The importer updates two Manager-owned files:

| File | Use |
| --- | --- |
| `model_catalog.json` | Resolved settings for every model and named profile, role mappings, runtime image, and dashboard summary. Manager plans and loads from this copy. |
| `remote_profiles.json` | The subset Manager offers to remote clients. The offer list lives in `tools/generate_profiles.py`; a registry profile alone does not publish a new route. |

Manager reads its imported copies at runtime; the rig does not need to read the
benchmarking checkout to answer a load request. Ordinary profiles give Manager
requirements such as context, batch, engine pack, and llama arguments. Manager
still chooses cards and context fit for the load.

Experimental CUDA kernels, tensor placement, Strata packs, and per-chat KV
workers start as benchmarking evidence with a runtime artifact and receipt.
Promote a configuration only when the runtime can execute it, the registry
expresses every required setting, the importer preserves it, and Manager knows
how to reserve the needed cards and launch it. The present registry has one
global `runtime_image`; a custom llama build or per-chat device map needs an
explicit runtime capability and Manager launch contract before it can become a
published profile. Do not encode an experimental placement as an ordinary
`tensor_split`: that controls model layer placement, not chat-owned KV.
