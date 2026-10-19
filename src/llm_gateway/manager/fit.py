"""Will this model fit on these cards, and at what context? The harness's own check (MANAGER.md rule 8).

The controller answers before anything reaches the rig. It reads the model's GGUF header (no weights)
and builds the memory llama.cpp will allocate per card:

- weights: every `blk.N.*` tensor goes to the card holding layer N; `token_embd` stays in host RAM
  (llama.cpp keeps the input layer on the CPU); `output` (or the tied embedding) goes to the last card;
  next-token-prediction layers (`nextn_predict_layers`) are skipped by llama.cpp and cost nothing.
- KV cache (q8_0, run_runtime.sh `-ctk/-ctv q8_0`): per attention layer, KV heads x (key + value
  length) x context. Hybrid models (`full_attention_interval`) keep KV only on their attention layers,
  recurrent layers hold a small fixed state; sliding-window layers hold at most the window.
- compute buffer per card: grows with context x micro-batch x heads (measured on the 1060s).
- a margin per card (llama.cpp's `--fit-target` idea) so a card never runs to its last MiB.

Layers are split in order across the cards in CUDA order (lowest GPU first), the output layer counting
against the last card. The plan is the largest context that fits (capped at the model's trained
context or the request), with the layers then balanced so every card has about the same headroom. The
result is exact llama.cpp arguments: `--ctx-size` and `--tensor-split` as whole layer counts (llama.cpp
assigns layer i to the card whose cumulative share first exceeds i / (layers + 1)).

Checked 2026-10-18 against llama.cpp b11669 on Ornith-1.5-9B over two 1060s: weights 4812 MiB and
KV 17.0 KiB per token predicted, the same as llama.cpp measured. The rig still runs llama.cpp's own
projection at load and refuses a load that would not fit, so this check can be conservative.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from typing import Any, BinaryIO

MIB = 1 << 20
KV_BYTES_PER_ELEM = 34 / 32  # q8_0: 32 values in 34 bytes
CTX_STEP = 256               # llama.cpp pads the context to 256
CTX_MIN_DEFAULT = 4096
MARGIN_MB_DEFAULT = 512
COMPUTE_BASE_MB = 64         # compute buffer at tiny context (measured 39 MiB on Ornith-9B)
CUDA_CONTEXT_MB = 150        # per card per process (CUDA context, cuBLAS workspace), not in llama.cpp's sums

# ggml type id -> (elements per block, bytes per block). Unknown ids count as 4 bytes per element.
GGML_BLOCKS = {
    0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22), 7: (32, 24), 8: (32, 34), 9: (32, 36),
    10: (256, 84), 11: (256, 110), 12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292),
    16: (256, 66), 17: (256, 74), 18: (256, 98), 19: (256, 50), 20: (32, 18), 21: (256, 110),
    22: (256, 82), 23: (256, 136), 24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8),
    29: (256, 56), 30: (1, 2), 34: (256, 54), 35: (256, 66), 39: (32, 17),
}
_SCALARS = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}


class FitError(Exception):
    """The GGUF could not be read."""


# ---------------------------------------------------------------- GGUF header

def _read(f: BinaryIO, fmt: str):
    size = struct.calcsize("<" + fmt)
    raw = f.read(size)
    if len(raw) != size:
        raise FitError("truncated GGUF header")
    return struct.unpack("<" + fmt, raw)[0]


def _string(f: BinaryIO) -> str:
    return f.read(_read(f, "Q")).decode("utf-8", "replace")


def _value(f: BinaryIO, vtype: int) -> Any:
    if vtype == 8:
        return _string(f)
    if vtype == 9:
        etype, n = _read(f, "I"), _read(f, "Q")
        if etype == 8:
            for _ in range(n):
                _string(f)
            return None  # string arrays (the vocabulary) are not needed
        if etype not in _SCALARS:
            raise FitError(f"unsupported GGUF array type {etype}")
        fmt = _SCALARS[etype]
        if n > 4096:  # big numeric arrays (token scores): skip
            f.seek(n * struct.calcsize("<" + fmt), os.SEEK_CUR)
            return None
        return [_read(f, fmt) for _ in range(n)]
    if vtype not in _SCALARS:
        raise FitError(f"unsupported GGUF value type {vtype}")
    return _read(f, _SCALARS[vtype])


def read_header(path: str) -> tuple[dict[str, Any], dict[str, int]]:
    """(metadata, tensor name -> bytes) from a GGUF file, reading only the header."""
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise FitError(f"{path} is not a GGUF file")
        _read(f, "I")  # version
        n_tensors, n_kv = _read(f, "Q"), _read(f, "Q")
        meta = {}
        for _ in range(n_kv):
            key = _string(f)
            meta[key] = _value(f, _read(f, "I"))
        tensors = {}
        for _ in range(n_tensors):
            name = _string(f)
            dims = [_read(f, "Q") for _ in range(_read(f, "I"))]
            ttype = _read(f, "I")
            _read(f, "Q")  # offset
            n = 1
            for d in dims:
                n *= d
            per_block, block_bytes = GGML_BLOCKS.get(ttype, (1, 4))
            tensors[name] = -(-n // per_block) * block_bytes
    return meta, tensors


# ---------------------------------------------------------------- model shape

@dataclass
class Shape:
    """Per-layer memory facts for one GGUF, independent of the cards."""
    model: str
    arch: str
    n_ctx_train: int
    n_head: int
    weights: list[int]            # bytes per layer slot (0 for skipped layers)
    kv_per_token: list[float]     # KV bytes per context token per layer (0 for non-attention layers)
    kv_window: list[int | None]   # sliding window per layer (None: full context)
    fixed_state: list[int]        # recurrent state bytes per layer (independent of context)
    output_bytes: int             # output layer, on the last card
    host_bytes: int               # token embedding, kept in host RAM
    notes: list[str] = field(default_factory=list)

    @property
    def n_layers(self) -> int:
        return len(self.weights)

    @property
    def weights_mb(self) -> float:
        return (sum(self.weights) + self.output_bytes) / MIB


def _per_layer(value: Any, n: int, default: Any) -> list:
    if isinstance(value, list):
        return (list(value) + [value[-1] if value else default] * n)[:n]
    return [default if value is None else value] * n


def shape_from_header(meta: dict[str, Any], tensors: dict[str, int], model: str = "") -> Shape:
    arch = meta.get("general.architecture") or ""
    k = lambda key, default=None: meta.get(f"{arch}.{key}", default)  # noqa: E731
    n_layer = int(k("block_count", 0))
    if n_layer <= 0:
        raise FitError(f"{model}: GGUF has no {arch}.block_count")
    notes = []
    nextn = int(k("nextn_predict_layers", 0) or 0)
    skipped = set(range(n_layer - nextn, n_layer)) if nextn else set()
    if skipped:
        notes.append(f"{nextn} next-token-prediction layer(s) not loaded")

    weights = [0] * n_layer
    attn_layers = set()
    for name, size in tensors.items():
        m = re.match(r"blk\.(\d+)\.", name)
        if not m:
            continue
        il = int(m.group(1))
        if il < n_layer and il not in skipped:
            weights[il] += size
            if ".attn_k." in name or ".attn_q." in name or ".attn_qkv." in name:
                attn_layers.add(il)

    n_head = int(_per_layer(k("attention.head_count", 1), 1, 1)[0] or 1)
    head_kv = _per_layer(k("attention.head_count_kv"), n_layer, n_head)
    emb = int(k("embedding_length", 0) or 0)
    key_len = int(k("attention.key_length", 0) or (emb // max(n_head, 1)))
    val_len = int(k("attention.value_length", 0) or key_len)

    interval = k("full_attention_interval")
    if interval:  # hybrid: KV only on every interval-th layer, recurrent state elsewhere
        is_attn = [(il + 1) % int(interval) == 0 for il in range(n_layer)]
        notes.append(f"hybrid: full attention every {interval} layers")
    elif attn_layers:
        is_attn = [il in attn_layers for il in range(n_layer)]
    else:
        is_attn = [True] * n_layer
    kv_per_token = [0.0] * n_layer
    for il in range(n_layer):
        if is_attn[il] and il not in skipped:
            kv_per_token[il] = float(head_kv[il] or 0) * (key_len + val_len) * KV_BYTES_PER_ELEM

    window = k("attention.sliding_window")
    pattern = k("attention.sliding_window_pattern")
    kv_window: list[int | None] = [None] * n_layer
    if window:
        if isinstance(pattern, list):
            swa = [bool(p) for p in _per_layer(pattern, n_layer, False)]
        elif pattern:
            swa = [(il + 1) % int(pattern) != 0 for il in range(n_layer)]
        else:
            swa = [False] * n_layer  # window without a pattern: treat as full context (conservative)
        kv_window = [int(window) if s else None for s in swa]
        if any(swa):
            notes.append(f"sliding window {window} on {sum(swa)} layers")

    fixed = [0] * n_layer
    inner, state = int(k("ssm.inner_size", 0) or 0), int(k("ssm.state_size", 0) or 0)
    if inner and state:
        conv, groups = int(k("ssm.conv_kernel", 4) or 4), int(k("ssm.group_count", 1) or 1)
        per = (conv - 1) * (inner + 2 * groups * state) * 4 + inner * state * 4
        fixed = [0 if (is_attn[il] or il in skipped) else per for il in range(n_layer)]

    output = tensors.get("output.weight")
    if output is None:
        output = tensors.get("token_embd.weight", 0)
        notes.append("tied embeddings: output layer reuses token_embd")
    output += sum(v for n, v in tensors.items() if n.startswith("output_norm") or n == "output.bias")
    return Shape(model=model, arch=arch, n_ctx_train=int(k("context_length", 0) or 0), n_head=n_head,
                 weights=weights, kv_per_token=kv_per_token, kv_window=kv_window, fixed_state=fixed,
                 output_bytes=int(output), host_bytes=int(tensors.get("token_embd.weight", 0)), notes=notes)


_SHAPES: dict[tuple[str, float], Shape] = {}


def shape_for(path: str, model: str = "") -> Shape:
    """Shape of a GGUF, cached per file and mtime (the header of a big vocabulary takes a moment)."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError as exc:
        raise FitError(f"{model or path}: cannot read {path}: {exc}") from exc
    if key not in _SHAPES:
        meta, tensors = read_header(path)
        _SHAPES[key] = shape_from_header(meta, tensors, model)
    return _SHAPES[key]


# ---------------------------------------------------------------- the plan

def _layer_bytes(s: Shape, il: int, ctx: int) -> float:
    tokens = ctx if s.kv_window[il] is None else min(ctx, s.kv_window[il])
    return s.weights[il] + s.kv_per_token[il] * tokens + s.fixed_state[il]


def compute_mb(s: Shape, ctx: int, ubatch: int) -> float:
    return COMPUTE_BASE_MB + ctx * ubatch * s.n_head * 4 / MIB


def _partition(s: Shape, ctx: int, budgets: list[float]) -> list[int] | None:
    """Contiguous layer counts per card that stay within each card's byte budget, filling cards in order
    (optimal for feasibility); the last card also carries the output layer. None if it cannot."""
    counts, il, n = [], 0, s.n_layers
    for d, budget in enumerate(budgets):
        room = budget - (s.output_bytes if d == len(budgets) - 1 else 0)
        if room < 0:
            return None
        start = il
        while il < n and _layer_bytes(s, il, ctx) <= room:
            room -= _layer_bytes(s, il, ctx)
            il += 1
        counts.append(il - start)
    return counts if il == n else None


def _card_use(s: Shape, ctx: int, counts: list[int]) -> list[dict[str, float]]:
    out, il = [], 0
    for d, c in enumerate(counts):
        layers = range(il, il + c)
        il += c
        w = sum(s.weights[i] for i in layers) + (s.output_bytes if d == len(counts) - 1 else 0)
        kv = sum(_layer_bytes(s, i, ctx) - s.weights[i] for i in layers)
        out.append({"layers": c, "weights_mb": w / MIB, "kv_mb": kv / MIB})
    return out


def plan(s: Shape, cards: list[dict[str, Any]], *, ctx: int | None = None, ctx_min: int = CTX_MIN_DEFAULT,
         ctx_max: int | None = None, ubatch: int = 64, parallel: int = 1,
         margin_mb: int = MARGIN_MB_DEFAULT) -> dict[str, Any]:
    """Fit `s` on `cards` ([{index, total_mb, used_mb}], any order; CUDA order is by index).

    ctx: an exact context that must fit; None: the largest that fits between ctx_min and ctx_max
    (default: the model's trained context). Returns {fits, ctx, tensor_split, cards[], reason, ...}.
    """
    cards = sorted(cards, key=lambda c: c["index"])
    gpus = [int(c["index"]) for c in cards]
    free = [float(c["total_mb"]) - float(c.get("used_mb", 0)) for c in cards]
    top = min(ctx_max or s.n_ctx_train or 131072, s.n_ctx_train or 1 << 30)
    want_lo, want_hi = (ctx, ctx) if ctx else (ctx_min, max(ctx_min, top))
    want_lo = max(CTX_STEP, want_lo // CTX_STEP * CTX_STEP)
    want_hi = max(want_lo, want_hi // CTX_STEP * CTX_STEP)

    def budgets(c: int, scale: float = 1.0) -> list[float]:
        return [(f - margin_mb - CUDA_CONTEXT_MB - compute_mb(s, c, ubatch)) * MIB * scale for f in free]

    base = {"model": s.model, "arch": s.arch, "gpus": gpus, "n_ctx_train": s.n_ctx_train,
            "weights_mb": round(s.weights_mb), "host_mb": round(s.host_bytes / MIB), "margin_mb": margin_mb,
            "parallel": parallel, "notes": s.notes}
    if not cards:
        return {**base, "fits": False, "reason": "no cards given"}
    if _partition(s, want_lo, budgets(want_lo)) is None:
        need = s.weights_mb + sum(s.kv_per_token[i] * (want_lo if s.kv_window[i] is None
                                                       else min(want_lo, s.kv_window[i])) + s.fixed_state[i]
                                  for i in range(s.n_layers)) / MIB
        have = sum(free) - len(cards) * (margin_mb + CUDA_CONTEXT_MB + compute_mb(s, want_lo, ubatch))
        why = (f"won't fit: {s.model} needs about {need:.0f} MiB on the cards at ctx {want_lo} "
               f"(weights {s.weights_mb:.0f} + KV/state {need - s.weights_mb:.0f}); "
               f"{len(cards)} card(s) {gpus} give {max(have, 0):.0f} MiB after margins")
        if have >= need:
            why += " in total, but the layers do not divide across the cards (a layer or the output is too big)"
        return {**base, "fits": False, "ctx": want_lo, "need_mb": round(need), "have_mb": round(have),
                "reason": why}
    lo, hi = want_lo, want_hi  # largest ctx that fits, by bisection on CTX_STEP multiples
    while lo < hi:
        mid = (lo + hi + CTX_STEP) // 2 // CTX_STEP * CTX_STEP
        if _partition(s, mid, budgets(mid)) is not None:
            lo = mid
        else:
            hi = mid - CTX_STEP
    best = lo
    # Balance: the smallest uniform share of each budget that still fits, so every card keeps headroom.
    scale_lo, scale_hi = 0.0, 1.0
    for _ in range(30):
        mid = (scale_lo + scale_hi) / 2
        if _partition(s, best, budgets(best, mid)) is None:
            scale_lo = mid
        else:
            scale_hi = mid
    blocks = _partition(s, best, budgets(best, scale_hi))
    split = blocks[:-1] + [blocks[-1] + 1]  # the output layer is the last slot llama.cpp splits
    use = _card_use(s, best, blocks)
    rows = []
    for c, f, u in zip(cards, free, use):
        comp = compute_mb(s, best, ubatch)
        total = u["weights_mb"] + u["kv_mb"] + comp + CUDA_CONTEXT_MB
        rows.append({"gpu": int(c["index"]), "total_mb": int(c["total_mb"]), "free_mb": round(f),
                     "layers": u["layers"], "weights_mb": round(u["weights_mb"]), "kv_mb": round(u["kv_mb"]),
                     "compute_mb": round(comp), "projected_mb": round(total), "headroom_mb": round(f - total)})
    capped = "requested" if ctx else ("trained context" if best >= top else "card memory")
    return {**base, "fits": True, "ctx": best, "ctx_limit": capped, "per_slot_ctx": best // max(parallel, 1),
            "tensor_split": ",".join(str(c) for c in split) if len(split) > 1 else None, "cards": rows,
            "reason": f"fits: {s.model} on GPU {gpus} at ctx {best} (limited by {capped})"}


def choose_cards(free_cards: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """n cards for a request that only gives a count: smallest cards first, so big cards stay free."""
    return sorted(free_cards, key=lambda g: (g["total_mb"], g["index"]))[:n]


def candidate_sets(free_cards: list[dict[str, Any]], n: int) -> list[list[dict[str, Any]]]:
    """Card sets of size n to try, in preference order: the n smallest (big cards stay free), then the
    biggest free card with the n-1 smallest others (one big card often beats several small ones)."""
    by_size = sorted(free_cards, key=lambda g: (g["total_mb"], g["index"]))
    sets = [by_size[:n]] if len(by_size) >= n else []
    if by_size and n <= len(by_size):
        big = by_size[-1]
        alt = [big] + [g for g in by_size if g is not big][:n - 1]
        if sorted(g["index"] for g in alt) != sorted(g["index"] for g in sets[0]):
            sets.append(alt)
    return sets


# Splitting across more cards costs speed (3090 + one 1060 2.6-3.1x slower, Nex on 4-5 1060s 6.5x;
# distributed_work_guide.md), so a model stays on the small cards only while it fits on this many of them.
SAME_CLASS_MAX_CARDS = 2


def choose(s: Shape, free_cards: list[dict[str, Any]], **kw: Any) -> dict[str, Any] | None:
    """The plan the harness picks when the request names no cards: the smallest card class that holds it on
    up to SAME_CLASS_MAX_CARDS cards (one card first), so big cards stay free for big models; failing that,
    the fewest cards of any mix. With an exact `ctx` that context must fit, otherwise at least ctx_min."""
    classes: dict[int, list[dict[str, Any]]] = {}
    for g in free_cards:
        classes.setdefault(int(g["total_mb"]), []).append(g)
    for size in sorted(classes):
        group = sorted(classes[size], key=lambda g: g["index"])
        for n in range(1, min(SAME_CLASS_MAX_CARDS, len(group)) + 1):
            p = plan(s, group[:n], **kw)
            if p["fits"]:
                return p
    for n in range(1, len(free_cards) + 1):
        for cards in candidate_sets(free_cards, n):
            p = plan(s, cards, **kw)
            if p["fits"]:
                return p
    return None


def suggest(s: Shape, free_cards: list[dict[str, Any]], **kw: Any) -> list[str]:
    """Alternatives for a request that does not fit: fewest small cards, or one card that holds it."""
    out = []
    for n in range(1, len(free_cards) + 1):
        pick = choose_cards(free_cards, n)
        p = plan(s, pick, **kw)
        if p["fits"]:
            out.append(f"{n} card(s) GPU {p['gpus']}: ctx {p['ctx']}")
            break
    for c in sorted(free_cards, key=lambda g: -g["total_mb"])[:1]:
        p = plan(s, [c], **kw)
        if p["fits"] and f"GPU {[c['index']]}" not in " ".join(out):
            out.append(f"1 card GPU {c['index']} ({c['total_mb']} MB): ctx {p['ctx']}")
    return out
