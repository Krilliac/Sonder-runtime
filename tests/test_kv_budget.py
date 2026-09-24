"""Pure KV-cache sizing and residency classification (domain.kv_budget)."""

import pytest

from sonder_runtime.domain import kv_budget as kb


def qwen_like(**overrides):
    info = {
        "general.architecture": "qwen2",
        "qwen2.block_count": 28,
        "qwen2.attention.head_count": 28,
        "qwen2.attention.head_count_kv": 4,
        "qwen2.embedding_length": 3584,
        "qwen2.context_length": 32768,
    }
    info.update(overrides)
    return info


def test_geometry_derives_head_dim_from_embedding_when_key_length_absent():
    geometry = kb.geometry_from_model_info(qwen_like())

    assert geometry.block_count == 28
    assert geometry.kv_heads_total == 28 * 4
    assert geometry.key_length == geometry.value_length == 128
    assert geometry.context_length == 32768


def test_per_token_cost_matches_hand_computation():
    geometry = kb.geometry_from_model_info(qwen_like())
    # 28 layers * 4 kv heads * (128 K + 128 V) * 2 bytes.
    assert kb.kv_bytes_per_token(geometry, "f16") == 28 * 4 * 256 * 2
    assert kb.kv_bytes_per_token(geometry, "q8_0") == pytest.approx(28 * 4 * 256 * 34 / 32)
    assert kb.kv_bytes(geometry, 8192, "f16") == 28 * 4 * 256 * 2 * 8192


def test_unknown_cache_type_is_costed_as_f16():
    geometry = kb.geometry_from_model_info(qwen_like())
    assert kb.kv_bytes_per_token(geometry, "mystery") == kb.kv_bytes_per_token(geometry, "f16")


def test_explicit_key_and_value_lengths_win():
    geometry = kb.geometry_from_model_info(qwen_like(**{
        "qwen2.attention.key_length": 192, "qwen2.attention.value_length": 128,
    }))
    assert (geometry.key_length, geometry.value_length) == (192, 128)


def test_per_layer_head_counts_cost_hybrid_models_correctly():
    heads = [0, 8] * 14
    geometry = kb.geometry_from_model_info(qwen_like(**{"qwen2.attention.head_count_kv": heads}))
    assert geometry.kv_heads_total == 8 * 14


@pytest.mark.parametrize("info", [
    None,
    {},
    qwen_like(**{"qwen2.block_count": 0}),
    qwen_like(**{"qwen2.attention.head_count_kv": [4] * 3}),
    qwen_like(**{"qwen2.attention.kv_lora_rank": 512}),
    {k: v for k, v in qwen_like().items() if k != "qwen2.embedding_length"},
])
def test_unmodelled_or_malformed_geometry_is_none(info):
    assert kb.geometry_from_model_info(info) is None


def test_ps_row_classification():
    resident = kb.reading_from_ps_row({"name": "m:7b", "size": 100, "size_vram": 100})
    hybrid = kb.reading_from_ps_row({"name": "m:7b", "size": 100, "size_vram": 60, "context_length": 8192})
    cpu = kb.reading_from_ps_row({"name": "m:7b", "size": 100, "size_vram": 0})

    assert resident.placement == kb.RESIDENT and resident.spilled_bytes == 0
    assert hybrid.placement == kb.HYBRID and hybrid.spilled_bytes == 40
    assert hybrid.context_length == 8192
    assert cpu.placement == kb.CPU_ONLY
    assert kb.reading_from_ps_row({"name": "m", "size": 100, "size_vram": True}) is None
    assert kb.reading_from_ps_row({"size": 100, "size_vram": 1}) is None


def test_spill_shrinks_window_by_measured_kv_with_margin():
    geometry = kb.geometry_from_model_info(qwen_like())
    per_token = kb.kv_bytes_per_token(geometry, "f16")
    spilled = per_token * 4000

    window = kb.window_after_spill(32768, spilled, geometry=geometry, kv_type="f16", minimum=512)

    assert window is not None and window % 1024 == 0
    assert window <= 32768 - 4000 * 1.10


def test_spill_larger_than_the_cache_is_unfixable():
    geometry = kb.geometry_from_model_info(qwen_like())
    spilled = kb.kv_bytes(geometry, 8192, "f16") * 2
    assert kb.window_after_spill(8192, spilled, geometry=geometry, kv_type="f16", minimum=512) is None


def test_spill_without_geometry_halves_and_respects_minimum():
    assert kb.window_after_spill(16384, 1, geometry=None, kv_type="f16", minimum=512) == 8192
    assert kb.window_after_spill(600, 1, geometry=None, kv_type="f16", minimum=512) == 512
    assert kb.window_after_spill(512, 1, geometry=None, kv_type="f16", minimum=512) is None
    assert kb.window_after_spill(8192, 0, geometry=None, kv_type="f16", minimum=512) is None
