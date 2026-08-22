"""Ownership and regression tests for the packaged model-sizing policy."""
from __future__ import annotations

import sonder_hardware
from sonder_runtime.domain import model_sizing


def test_model_tag_parser_is_owned_by_domain_and_reexported_for_compatibility():
    assert sonder_hardware.params_from_model_tag is model_sizing.params_from_model_tag


def test_model_band_policy_is_owned_by_domain_and_reexported_for_compatibility():
    assert sonder_hardware.decode_band is model_sizing.decode_band
    assert sonder_hardware.memory_band is model_sizing.memory_band
    assert sonder_hardware.band_fits is model_sizing.band_fits
    assert sonder_hardware.estimated_footprint_gb is model_sizing.estimated_footprint_gb


def test_domain_model_band_policy_preserves_moe_sizing_behavior():
    assert model_sizing.decode_band(3.3) == "3-4B"
    assert model_sizing.memory_band(30.0) == "13-34B"
    assert model_sizing.band_fits("13-34B", "13-34B") is True
    assert model_sizing.band_fits("13-34B", "7B") is False
    assert model_sizing.estimated_footprint_gb(30.0) == 18.6
    assert model_sizing.decode_band("unknown") is None
    assert model_sizing.memory_band(0) is None
    assert model_sizing.band_fits("unknown", "7B") is None
    assert model_sizing.estimated_footprint_gb(None) is None


def test_domain_parser_handles_moe_dense_and_alias_tags():
    assert model_sizing.params_from_model_tag("qwen3-coder:30b-a3b-q4_K_M") == (30.0, 3.0)
    assert model_sizing.params_from_model_tag("qwen2.5-coder:14b") == (14.0, 14.0)
    assert model_sizing.params_from_model_tag("custom-70b-model:latest") is None
    assert model_sizing.params_from_model_tag("bogus:8b-a9b") is None
