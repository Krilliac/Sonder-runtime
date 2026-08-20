"""Ownership and regression tests for the packaged model-sizing policy."""
from __future__ import annotations

import sonder_hardware
from sonder_runtime.domain import model_sizing


def test_model_tag_parser_is_owned_by_domain_and_reexported_for_compatibility():
    assert sonder_hardware.params_from_model_tag is model_sizing.params_from_model_tag


def test_domain_parser_handles_moe_dense_and_alias_tags():
    assert model_sizing.params_from_model_tag("qwen3-coder:30b-a3b-q4_K_M") == (30.0, 3.0)
    assert model_sizing.params_from_model_tag("qwen2.5-coder:14b") == (14.0, 14.0)
    assert model_sizing.params_from_model_tag("custom-70b-model:latest") is None
    assert model_sizing.params_from_model_tag("bogus:8b-a9b") is None
