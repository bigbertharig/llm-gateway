# Automatic host RAM spillover trial ? 2026-11-03

Rig: RTX 3090 Ti alone; five GTX 1060 cards temporarily reserved to exercise the fallback. Reservation and model were released after the trial. The rig returned to idle.

## Result

- Qwen3.6 35B-A3B UD-Q4_K_XL, requested context 262,144, chat use.
- Gateway selected host RAM only after no available full-GPU fit. Planned 23 GPU layers, 12,731 MiB host estimate, 2,048 MiB GPU margin.
- Rig loaded at context 262,144 and reported 23 of 42 layers on GPU 0; measured GPU model 11,220 MiB, KV 1,632 MiB, compute 1,116 MiB, host 10,341 MiB.
- A direct short chat returned the word ready, finish reason stop, 17 prompt tokens and 2 completion tokens in 2.7 s. This checks serving, not long-context recall or throughput.
- Host MemAvailable after load was 27,156 MiB; swap free 23,077 MiB. Container swap was disabled by setting its memory-swap limit equal to its 20g memory limit.
- Model unloaded and temporary GPU claim released.
- The live load was run before the final RAM-path prompt-cache change. The final code sets host prompt cache to 0 for partial offload so it cannot consume the remaining container budget; that change is covered by the gateway integration test but was not rerun live.

## Failed calibration trial

Nemotron 3.5 Bolt 74B-A9B at context 32,768 was initially planned at 53 GPU layers with a 512 MiB planning margin. llama.cpp failed while allocating a 611 MiB CUDA compute buffer. The rig never marked it ready and removed the failed runtime. The RAM planner now requires at least 2,048 MiB GPU margin; at that margin this specific one-GPU placement does not fit the 18 GiB host admission budget. Previous benchmark evidence for 48 GPU layers remains separate from this gateway trial.

## Scope

Automatic fallback currently applies to unpinned interactive chat at exact or profile working context. Explicit GPU placement and benchmarks retain the full-GPU path. Strata is not selected automatically; it has only benchmark launch scripts, not a rig-owned load backend with lease, stop, readiness and memory validation.
