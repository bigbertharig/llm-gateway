# Shared Qwen 27B slots on the mixed GPU rig (2026-11-06)

## Question

Can one Qwen3.6 27B weight load on the RTX 3090 Ti serve four long conversations, with each conversation's KV/context held independently on one GTX 1060? If 250K each does not fit, find a smaller useful per-chat cap.

## Runtime and scope

Rig: GPU 0 RTX 3090 Ti (24,564 MiB), GPUs 1-5 GTX 1060 (6,144 MiB each). Runtime: llama.cpp b11669, Qwen3.6 27B Q4_K_M weights. The registry defaults to Q8_0 for both K and V caches. Loads and unloads went through the Pi gateway manager; no raw harness repository or runtime code was changed. The manager fit and measured per-device allocations are the memory evidence. Synthetic prompts establish concurrent serving and isolation; they do not establish useful 250K retrieval quality.

## Results

| Configuration | Slot cap | Active prompt test | Wall time | GPU allocation measured at load | Result |
| --- | ---: | --- | ---: | --- | --- |
| 3090 only, 2 slots, unified KV | 131,072 each | 2 short marker prompts | 2.99 s | GPU0: weights 15,346, KV 4,352, compute 541 MiB | Both correct |
| 3090 + GPU1, 2 slots, unified KV | 262,144 each | 2 short marker prompts | 5.64 s | GPU0: weights 12,921, KV 7,616, compute 1,179; GPU1: weights 2,424, KV 1,088, compute 1,168 MiB | Both correct |
| 3090 + GPU1, 2 slots, unified KV | 262,144 each | 2 simultaneous ~70,032-token prompts | 550.2 s | Same layout | Both markers correct, no cross-talk; requests completed at 190 and 550 s |
| 3090 + GPU1, 4 slots, unified KV with per-slot limit | 65,536 each | 4 simultaneous ~7,031-token prompts | 60.07 s | GPU0: weights 12,675, KV 7,616, compute 1,179; GPU1: weights 2,670, KV 1,088, compute 1,168 MiB | All four correct, no cross-talk |
| 3090 only, 4 slots, unified KV with per-slot limit | 32,768 each | Same 4 simultaneous ~7,031-token prompts | 29.45 s | GPU0: weights 15,346, KV 4,352, compute 541 MiB | All four correct, no cross-talk |

The different slot caps in the 2-slot rows show the configured slot limit, not the amount of conversation text tested. The four-slot tests likewise establish four concurrent slots and ~7K actual prompt tokens each; a full 4 x 64K fill remains untested. The two 70K prompts test substantial simultaneous context but one request took much longer, indicating contention/serialized prefill in this layout. The 3090-only configuration served the identical four prompts about 2.0x faster than the 3090+1060 configuration, at half the per-slot cap.

Receipts: `/home/bryan/llm-harness/results/parallel_slots_20261106/`, especially `qwen27_gpu0_1_long.json`, `qwen27_gpu0_1_four_slots_7k.json`, and `qwen27_gpu0_four_slots_7k.json`. The earlier 1-slot and 2-slot receipts are in the same directory.

## Placement finding

The current llama.cpp multi-GPU modes cannot express "one weight copy on GPU0, slot A's entire KV on GPU1, slot B's on GPU2," etc. Layer split allocates each layer's KV on the device owning that layer. The measured two-card load confirms GPU1 held 2.4-2.7 GiB of weights and ~1.1 GiB of KV shared by the loaded model, not one isolated conversation. Tensor split and row split also do not provide per-slot GPU affinity. `--kv-unified-per-slot` sets a per-slot context limit in a shared pool; it does not set a device for that slot. `/slots` reported four 65,536-token slots on the two-card load and four 32,768-token slots on the one-card load.

At Q8_0 K/V, the measured 262,144-token pool needs 8,704 MiB of KV across the two cards. A single ~250K-token chat would therefore need roughly 8,300 MiB (8.1 GiB) of KV (linear estimate), already more than a 6 GiB 1060 before runtime overhead. Q4_0 K/V would be nominally about half as large (~4,150 MiB for 250K tokens) and may fit in 6 GiB in isolation, but this is **unmeasured** and the installed manager does not account for cache-type changes in its fit gate. Accuracy and speed of quantized KV also need measurement. No 4 x 250K load or 1M aggregate pool was demonstrated.

The model metadata reports 262,144 training context per sequence. The manager currently treats that as a maximum **total** `ctx_size`, although a four-slot pool may reasonably need up to 4 x the per-sequence cap. Manager fit/admission must distinguish total pool tokens from per-slot tokens before attempting a larger pool. The runtime supports `--kv-unified-per-slot N`; when `--ctx-size` is omitted, its documented pool size is N times the slot count.

## Next tests and engineering

1. Add a manager profile/load request that states slot count, per-slot context cap, total KV pool, and K/V cache types separately. Fit per-device memory using those settings and reject unsafe loads before starting the runtime. Keep the raw llama.cpp and harness repos unchanged.
2. Compare Q8_0 and Q4_0 cache at the same four-slot workload: actual VRAM, answer quality on long-context retrieval, prefill/decode throughput, and safe context caps. The 3090-only 4 x 32K and two-card 4 x 64K results are baselines.
3. Fill all four slots near their caps with separate multi-turn conversations and test prefix reuse, slot affinity, eviction, reload, and no cross-chat leakage. Include capacity/throughput receipts; a configured cap alone is insufficient.
4. If true per-1060 KV ownership is still desired, investigate runtime changes or another server backend that explicitly supports per-sequence KV placement with attention computation on that device. A simple manager flag cannot add this to existing llama.cpp placement modes.
5. Let the manager choose a measured capacity/speed layout and return ready plus actual per-chat cap to the chat/harness. Do not have chat code reason about GPU placement.

Primary runtime references: https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md and https://github.com/ggml-org/llama.cpp/blob/master/docs/multi-gpu.md.


## Four-conversation continuation check

On the 3090-only four-slot load, a second turn was sent for all four marker conversations concurrently, with the prior user prompt and assistant reply included in each history. All four returned the correct marker with no cross-chat marker in the reply. The server reported 7,032-7,036 cached prompt tokens per request and only 24 new prompt tokens each; all four completed in 2.8 seconds wall time. Receipt: `qwen27_gpu0_four_slots_continuation.json`. This demonstrates prompt-prefix reuse and effective conversation continuity for four short histories. It does not prove a fixed slot-to-GPU assignment, and a server reload would still lose in-memory KV unless an explicit cache save/restore path is added.


## Near-capacity four-slot check

With Qwen 27B on GPU0 only, four concurrent prompts of 30,131-30,133 tokens each (about 120,526 aggregate prompt tokens) all returned the correct separate marker, with no cross-chat marker. The shared Q8_0 KV pool was 131,072 tokens, and `--kv-unified-per-slot 32768` reported four 32,768-token slot caps. Total wall time was 166.59 seconds; individual completions arrived between 31.31 and 166.59 seconds, showing substantial queueing/serialized prefill. This is an actual near-capacity fill, unlike the earlier 7K-per-slot check. Receipt: `qwen27_gpu0_four_slots_30k.json`. No 4 x 64K near-capacity fill has yet been run.


## Follow-up: Q4 KV and a larger one-card pool (2026-11-06)

The target was revised to about 100K context per chat if it fits. Qwen 27B weights remained entirely on GPU0 (3090 Ti). Q4_0 K/V cache at a 201,728-token pool loaded with 3,546 MiB of KV, compared with 6,698 MiB Q8_0 KV in the normal chat load. A 400,000-token total pool with four 100K slots failed at startup when CUDA could not reserve a ~1,625 MiB compute buffer; reducing batch size from 64 to 32 did not fix that allocation. The manager released both failed loads without a leftover lease.

A 336,000-token requested pool at batch 32 loaded; llama.cpp rounded to 336,128 total tokens and reported four 84,000-token slots. Measured GPU0 allocations: model 15,346 MiB, KV 5,908 MiB, compute 1,340 MiB; `nvidia-smi` showed 23,535 MiB used and only 578 MiB free. GPU1-5 remained empty. This is a narrow experimental fit, not a safe default. It also shows that the user's desired aggregate context can be made substantially larger while keeping one weight load on the 3090, without using a 1060.

Four concurrent ~59,531-token first turns (238,126 aggregate prompt tokens) completed in 570.45 seconds. All returned their own marker correctly, with no cross-chat marker. The slow wall time reflects mostly serial long-prefill work. Receipts: `qwen27_gpu0_q4_four_slots_60k.json` and the dynamic manager load observation.

A second turn for all four returned correct answers, but one prefix was fully reprocessed and the group took 212 seconds. Three requests showed ~59.5K cached prompt tokens; one showed zero cached tokens. The server was launched with the gateway's 4,096 MiB RAM prompt cache and llama.cpp's default idle-slot cache behavior. We are testing `--no-cache-idle-slots --cache-ram 0` to see if keeping the four live slots in the GPU KV pool prevents that eviction. Receipt: `qwen27_gpu0_q4_four_slots_60k_continuation.json`.

A separate attempt to use deprecated `--split-mode row --main-gpu 1` for weights on GPU0 and KV on GPU1 failed to load on this CUDA build: `device CUDA0 does not support split buffers`. The supported layer mode co-locates each layer's weights and KV, and the tested row mode is unavailable. No test has placed an entire chat's KV on its own 1060 while sharing one 3090 weight load.

**Manager correctness gap:** For this controlled experiment, `rigctl load-model` was asked for 201,728 tokens to pass its Q8_0 fit gate, while extra llama.cpp arguments selected Q4_0 and overrode the runtime total context to 336,000. The manager's requested-context estimate is therefore not the actual runtime context. A production profile must make cache type, total pool, per-slot cap, and slot count first-class fit inputs and validate the measured runtime value before returning ready. Do not copy this override method into normal chat loads.


The no-idle-cache retest used the same 336,128-token Q4 pool and four 84K slots, but launched llama.cpp with `--no-cache-idle-slots --cache-ram 0`. Four ~59.5K-token first turns again completed correctly (571.62 seconds). On the second turn, **all four** reused 59,532-59,536 cached tokens and answered correctly in **2.28 seconds total**. This confirms that the earlier 212-second outlier was a cache-retention policy effect, not a fundamental multi-chat limit at this size. Receipt: `qwen27_gpu0_q4_four_slots_60k_no_idle_cache.json`. The manager and chat integration should deliberately choose a cache policy for long-lived conversations; a large advertised window alone does not guarantee fast continuation.


## Split GPU stage: 3090 + one 1060

With layer split on GPUs 0 and 1, Q4_0 K/V, batch 32, no idle-slot cache, and `--kv-unified-per-slot 100000`, llama.cpp loaded a 400,128-token total pool and four 100,000-token slots. Measured allocation: GPU0 weights 12,430, KV 6,154, compute 1,675 MiB; GPU1 weights 2,916, KV 879, compute 1,669 MiB. `nvidia-smi` showed 444 MiB free on GPU1 after load. This is one shared model with weights and per-layer KV spread over both cards, not one chat's KV residing wholly on GPU1.

The same four ~59.5K-token first turns all answered correctly without cross-chat markers. They took **1,658.01 seconds (27.6 minutes)**, compared with **571.62 seconds (9.5 minutes)** on GPU0 alone with the same Q4 cache and no-idle-cache setting: 2.90x slower. Four continuations all reused 59,532-59,536 cached tokens, answered correctly, and took **4.27 seconds total**, versus **2.28 seconds** on GPU0 alone: 1.87x slower. This validates shared weights, four independent long chat histories, and cache retention across two GPUs, while showing a major throughput penalty on this PCIe/1060 layer path. Receipt: `qwen27_gpu0_1_q4_four_slots_60k_no_idle_cache.json`.

The larger runtime pool again came from an extra llama.cpp `--ctx-size 400000` argument after the manager's 262,144-token admission value; the manager must be taught aggregate pool and cache-type accounting before this can become an official profile.


## All six GPUs: five-chat capacity sweep

The 3090 plus all five 1060s loaded Qwen 27B with Q4_0 K/V and a 600,064-token pool as four 150K slots. Measured per-device total allocations (MiB): GPU0 15,046; GPUs1-5 5,227 / 4,602 / 5,227 / 5,097 / 4,998. Two 1060s had ~694 MiB free. Four distinct short marker prompts all passed. Receipt: `qwen27_gpu0_1_2_3_4_5_q4_4x150k_smoke.json`.

Five 200K slots (1,000,000 total tokens) failed to load. CUDA could not reserve a ~3,929 MiB compute buffer on GPU1. The same aggregate size had already failed with four 250K slots. Lowering to five 130K slots succeeded: runtime total 650,240 tokens, with 130,000-token caps reported by all five slots. Measured total allocations (MiB): GPU0 15,696; GPUs1-5 5,547 / 4,867 / 5,547 / 5,417 / 5,263. `nvidia-smi` showed only 360 MiB free on GPUs1 and 3, so this is a tight capacity edge. Five concurrent ~7,031-token first turns all answered correctly without cross markers, taking 956.1 seconds overall. Five second turns all answered correctly, each reused ~7,032-7,036 cached tokens, and took 7.8 seconds overall. Receipt: `qwen27_gpu_all_q4_5x130k_7k.json`. Full 130K fills remain untested.

These are five logical server slots sharing one 650K KV pool. The slots are **not** pinned one per 1060; in layer mode every GPU holds the KV belonging to the model layers on that GPU. GPU0 still holds most model weights and a substantial part of KV. This setup answers aggregate capacity, not the proposed per-1060 KV ownership.


The next load-only sweep held the same six GPUs, Q4_0 cache, batch 8, no idle-slot cache, and changed the number/size of slots:

| Configured slots | Runtime total context | Launch | Minimum observed free VRAM on a 1060 | Request check |
| --- | ---: | --- | ---: | --- |
| 5 x 200,000 | 1,000,000 requested | Failed: CUDA1 compute-buffer allocation (~3,929 MiB) | n/a | Not run |
| 5 x 140,000 | 700,160 | Loaded | 52 MiB | Deliberately not run: no practical margin |
| 5 x 130,000 | 650,240 | Loaded | 360 MiB | 5 x ~7K first turns and continuations passed |
| 4 x 150,000 | 600,064 | Loaded | 694 MiB | 4 short prompts passed |
| 6 x 100,000 | 600,064 | Loaded | 644 MiB | 6 x ~100-token first turns and continuations passed |

The six-slot load measured total allocations (MiB): GPU0 15,066; GPUs1-5 5,246 / 4,621 / 5,246 / 5,116 / 5,017. Six distinct first turns all answered correctly in 17.28 seconds; six continuations all reused their prefixes and answered correctly in 8.27 seconds. Receipt: `qwen27_gpu_all_q4_6x100k_smoke.json`. Thus six 100K *configured caps* fit and are callable; six nearly full 100K histories were not exercised. Five 130K is the largest tested configuration that has answered multi-turn requests. The 140K cap is only a startup proof with unsafe headroom. Neither the 5-slot nor 6-slot setup binds a slot to a particular 1060.

The repeated 1M failure is at CUDA compute-buffer reservation on a 1060, not just the Q4 KV footprint. The current layer split replicates a context-dependent compute allocation on each participating GPU. More 1060s can therefore reduce available headroom even as they add nominal VRAM. A later placement study can skew the layer split toward the 3090 and test whether that changes the bound, followed by GPU+RAM and Strata tiers in that order. The present sweep did not perform those optimization stages.

## Per-chat 1060 cache worker feasibility (2026-11-06)

The placement objective is one model weight load on GPU0 (3090 Ti) and one
live chat cache plus attention worker on each of GPUs1-5 (1060s). This differs
from llama.cpp's existing layer split: each chat would keep its own KV on its
assigned card while sharing GPU0's weights. The design should apply to dense
and MoE models; expert placement is independent.

Benchmarking's
`KV_PER_CHAT_GPU_PLACEMENT_2026-11-06.md` and
`results/runtime_experiments/kv_cache_residency_20261106/` hold the new
source and receipts. The Qwen 27B measured Q4_0 KV rate (5,908 MiB for
336,128 tokens) scales to about 4,395 MiB at 250K. All five 1060s
simultaneously reserved 4,395 MiB of cache plus 1,024 MiB workspace, leaving
576 MiB free each. The same cache with 1,536 MiB workspace also allocated,
but left only 64 MiB free and is unsuitable as a serving target. At 200K,
3,515 MiB cache plus 1,536 MiB workspace left 944 MiB free.

A standalone CUDA worker then computed one FP16 attention head against a
separate 100K/200K/250K resident token array on each 1060. GPU0 supplied
the query and received all five results through pinned host memory; all
five outputs verified. Five-worker median round trips were 1.30/2.44/3.05
ms respectively. The worker reserved a Q4-sized whole-chat cache but only
read one head's 49/98/122 MiB of FP16 K/V, so this is a placement and
transport proof, not full-model inference or a 250K retrieval result.
The existing 27B chat route stayed loaded and all five 1060s returned to
idle memory use after the probe.

The next gate was to fork llama.cpp so each context could assign KV and
attention to a 1060 while weights stayed on GPU0. That short-prompt gate
passed as described below. Near-capacity conversations, continuation reuse,
and isolation remain to be tested. Current `--override-tensor`, layer split,
and Strata flags cannot express this per-context GPU assignment.

The final 250K worker run gave each 1060 a different cache value (1 through 5)
and verified all five returned results on GPU0 in their correct lanes. Its
10-iteration median round trip was 3.05 ms. Receipt:
`results/runtime_experiments/kv_cache_residency_20261106/attention_250000_distinct.json`
in the benchmarking repository. This checks the synthetic lane mapping; it
does not establish real conversation isolation or full-model generation.

## Real-model per-chat KV proof (2026-11-06)

A private llama.cpp prototype now shares one model object on GPU0 across five
contexts. Each context stores KV and runs flash attention on its assigned 1060.
Five distinct short chats passed on dense Qwen3 1.7B, MoE GPT-OSS 20B, and the
original dense Qwen3.6 27B, with 32,768-token cache allocations per chat. The
27B run peaked at 19,019 MiB on GPU0 and 2,208 MiB on each 1060; each chat
produced its expected capital as the first token. The existing `chat-27b` route
was restored after testing.

This proves multiple independent resident KV windows sharing one real model
weight load across the six cards. The harness decodes them sequentially and
filled only short prompts, so long-context quality, near-full cache use, and
concurrent serving remain open. The exact private patch, model logs, GPU samples,
and summary are in benchmarking at
`results/runtime_experiments/kv_chat_real_model_20261106/`. The per-chat mapping
is still an experimental runtime change and is not a Manager load profile.
