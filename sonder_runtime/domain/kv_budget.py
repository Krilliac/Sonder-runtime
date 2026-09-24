"""Pure KV-cache sizing and measured-residency policy.

Sonder chooses a native context window for each local model.  The memory that
window costs is the KV cache, whose size depends on the model's attention
geometry and on the cache element type the *model server* was started with.
Two facts make that easy to get silently wrong:

* ``OLLAMA_KV_CACHE_TYPE`` configures the Ollama server process.  Sonder's own
  environment is not the server's environment (tray apps, services and remote
  hosts all start Ollama independently), so reading it locally is a guess.
* A window that does not fit does not fail; Ollama spills layers to system RAM
  and generation slows by an order of magnitude with no error.

This module therefore answers two narrow questions from measured inputs:

1. How many bytes of KV cache does one token cost for a model whose geometry
   Ollama reported in ``/api/show`` ``model_info``?
2. Given a measured ``/api/ps`` row, is the model fully resident, and if it
   spilled, what smaller window would remove the spill?

Estimates are deliberate upper bounds: sliding-window layers are costed as full
attention, so a window derived here never exceeds what fits.  Architectures
whose cache layout is not modelled (multi-head latent attention) return
``None`` rather than a fabricated number, and callers keep their existing
fallback.  Nothing here performs I/O, reads the environment or keeps state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

# Bytes per cached element.  Quantised llama.cpp cache types store blocks of
# 32 values: q8_0 is 32 int8 + one fp16 scale (34 bytes); q4_0 is 16 bytes of
# nibbles + one fp16 scale (18 bytes); the _1 variants add an fp16 minimum and
# q5 variants add 4 bytes of high bits.
KV_BYTES_PER_ELEMENT: Mapping[str, float] = {
    "f32": 4.0,
    "f16": 2.0,
    "bf16": 2.0,
    "q8_0": 34 / 32,
    "q5_1": 24 / 32,
    "q5_0": 22 / 32,
    "q4_1": 20 / 32,
    "q4_0": 18 / 32,
}
QUANTISED_KV_TYPES = frozenset({"q8_0", "q5_1", "q5_0", "q4_1", "q4_0"})
DEFAULT_KV_TYPE = "f16"

RESIDENT = "gpu-resident"
HYBRID = "gpu+ram-hybrid"
CPU_ONLY = "cpu"

# A spill measurement includes compute buffers that shrink less than linearly
# with the window, so a clamp removes 10% more KV than the observed overflow
# and rounds down to a whole 1024-token page.
_SPILL_MARGIN = 1.10
_WINDOW_PAGE = 1024


def normalize_kv_type(value: object) -> str | None:
    """Return the canonical cache type name, or ``None`` if unrecognised."""
    text = str(value or "").strip().lower()
    return text if text in KV_BYTES_PER_ELEMENT else None


@dataclass(frozen=True)
class ModelGeometry:
    """Attention geometry that determines per-token KV-cache cost.

    ``kv_heads_per_layer`` holds one entry per transformer block, so hybrid
    models whose recurrent blocks keep no attention cache (zero KV heads) are
    costed correctly.
    """

    architecture: str
    block_count: int
    kv_heads_per_layer: tuple[int, ...]
    key_length: int
    value_length: int
    context_length: int | None = None

    @property
    def kv_heads_total(self) -> int:
        return sum(self.kv_heads_per_layer)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _per_layer_heads(value: Any, block_count: int) -> tuple[int, ...] | None:
    """Expand a scalar or per-layer head count to exactly ``block_count``."""
    scalar = _positive_int(value)
    if scalar is not None:
        return (scalar,) * block_count
    if isinstance(value, (list, tuple)) and len(value) == block_count:
        heads = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
            heads.append(item)
        return tuple(heads) if any(heads) else None
    return None


def geometry_from_model_info(info: Mapping[str, Any] | None) -> ModelGeometry | None:
    """Parse ``/api/show`` ``model_info`` into a :class:`ModelGeometry`.

    Returns ``None`` when a required key is missing or malformed, or when the
    architecture uses multi-head latent attention, whose compressed cache is
    not modelled here.
    """
    if not isinstance(info, Mapping):
        return None
    architecture = info.get("general.architecture")
    if not isinstance(architecture, str) or not architecture.strip():
        return None
    architecture = architecture.strip()

    def key(name: str) -> Any:
        return info.get(f"{architecture}.{name}")

    if key("attention.kv_lora_rank") is not None:
        return None
    block_count = _positive_int(key("block_count"))
    if block_count is None:
        return None
    head_count_raw = key("attention.head_count")
    head_count = _positive_int(head_count_raw)
    kv_raw = key("attention.head_count_kv")
    kv_heads = _per_layer_heads(kv_raw if kv_raw is not None else head_count_raw, block_count)
    if kv_heads is None:
        return None
    key_length = _positive_int(key("attention.key_length"))
    if key_length is None:
        embedding = _positive_int(key("embedding_length"))
        if embedding is None or head_count is None or embedding % head_count:
            return None
        key_length = embedding // head_count
    value_length = _positive_int(key("attention.value_length")) or key_length
    return ModelGeometry(
        architecture=architecture,
        block_count=block_count,
        kv_heads_per_layer=kv_heads,
        key_length=key_length,
        value_length=value_length,
        context_length=_positive_int(key("context_length")),
    )


def kv_bytes_per_token(geometry: ModelGeometry, kv_type: str) -> float:
    """Upper-bound KV bytes one token of context costs across all layers."""
    element = KV_BYTES_PER_ELEMENT.get(normalize_kv_type(kv_type) or DEFAULT_KV_TYPE)
    return geometry.kv_heads_total * (geometry.key_length + geometry.value_length) * element


def kv_bytes(geometry: ModelGeometry, context: int, kv_type: str) -> int:
    """Upper-bound KV bytes for a ``context``-token window."""
    return int(math.ceil(kv_bytes_per_token(geometry, kv_type) * max(0, int(context))))


@dataclass(frozen=True)
class ResidencyReading:
    """One model's measured placement from an Ollama ``/api/ps`` row."""

    model: str
    total_bytes: int
    vram_bytes: int
    context_length: int | None

    @property
    def placement(self) -> str:
        if self.vram_bytes <= 0:
            return CPU_ONLY
        if self.vram_bytes >= self.total_bytes:
            return RESIDENT
        return HYBRID

    @property
    def spilled_bytes(self) -> int:
        return max(0, self.total_bytes - max(0, self.vram_bytes))


def reading_from_ps_row(row: Mapping[str, Any] | None) -> ResidencyReading | None:
    """Parse one ``/api/ps`` model row; ``None`` when it proves nothing.

    ``context_length`` is reported by Ollama releases that expose the loaded
    window; older releases omit it and callers must attribute the reading to
    the window they last requested.
    """
    if not isinstance(row, Mapping):
        return None
    name = str(row.get("name") or row.get("model") or "").strip()
    total = _positive_int(row.get("size"))
    vram = row.get("size_vram")
    if not name or total is None or isinstance(vram, bool) or not isinstance(vram, int) or vram < 0:
        return None
    return ResidencyReading(
        model=name,
        total_bytes=total,
        vram_bytes=vram,
        context_length=_positive_int(row.get("context_length")),
    )


def window_after_spill(
    context: int,
    spilled_bytes: int,
    *,
    geometry: ModelGeometry | None,
    kv_type: str,
    minimum: int,
) -> int | None:
    """Return a smaller window expected to remove ``spilled_bytes``.

    ``None`` means shrinking the window cannot fix the spill: the geometry
    shows the overflow is larger than the cache that could be given up, so the
    weights themselves exceed the GPU and only a smaller model or quantisation
    helps.  Without geometry the window halves, which converges in a few
    observations and never goes below ``minimum``.
    """
    context = int(context)
    minimum = max(1, int(minimum))
    if spilled_bytes <= 0 or context <= minimum:
        return None
    if geometry is None:
        target = context // 2
    else:
        per_token = kv_bytes_per_token(geometry, kv_type)
        if per_token <= 0:
            return None
        drop = int(math.ceil(spilled_bytes * _SPILL_MARGIN / per_token))
        if drop >= context - minimum:
            return None
        target = context - drop
    target = (target // _WINDOW_PAGE) * _WINDOW_PAGE if target >= _WINDOW_PAGE else target
    target = max(minimum, target)
    return target if target < context else None


__all__ = [
    "CPU_ONLY",
    "DEFAULT_KV_TYPE",
    "HYBRID",
    "KV_BYTES_PER_ELEMENT",
    "ModelGeometry",
    "QUANTISED_KV_TYPES",
    "RESIDENT",
    "ResidencyReading",
    "geometry_from_model_info",
    "kv_bytes",
    "kv_bytes_per_token",
    "normalize_kv_type",
    "reading_from_ps_row",
    "window_after_spill",
]
