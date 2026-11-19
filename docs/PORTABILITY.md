# Porting the Big Bertha design to another rig

The four repositories are an integrated home-lab system, not a ready-made installer for arbitrary hardware. The useful portable parts are the published model-evidence format, the Gateway API and test fakes, the reviewed tool catalog, and the rig admission/observation contracts. The deployment scripts also assume a Pi control host, an isolated rig link, private host inventory, and particular mounts and model files.

## What to supply and verify

1. Supply private host inventory, addresses, mount roots, credentials, and model files for the new site. Do not copy the published examples as literal production configuration.
2. Run Benchmarking's [rig survey and synthetic work scan](https://github.com/bigbertharig/benchmarking/blob/master/scripts/integration/README.md) on the new machine. Record per-card VRAM and identity, host RAM, storage, and link/transfer behavior. VRAM is the first fit input; runtime support, RAM/storage headroom, and context/KV reservations can also block a load.
3. Import the reviewed [model library](https://github.com/bigbertharig/benchmarking/blob/master/CROSS_REPO_CONTRACT.md), then remeasure load, context, speed, concurrency, and quality for recipes you intend to offer. A recipe measured on Big Bertha is a candidate on another card mix, not a guaranteed placement.
4. Test Gateway's fake-rig contracts before enabling a live rig. Then test one model load and release, failure/recovery, and concurrent admission on the actual hardware. Legacy NFS queue users should separately test competing task claims and worker loss on their own NFS server and mount options.

The current rig uses NVIDIA tooling and custom runtime paths in some experiments. Other vendors or a single-box deployment may need different launch, telemetry, power, and network adapters. The OpenAI-compatible API does not by itself make those hardware scripts portable. See [the current architecture](../ARCHITECTURE.md) and [Manager guide](MANAGER.md) for the boundary being migrated.
