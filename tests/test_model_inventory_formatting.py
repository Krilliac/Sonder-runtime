"""Unit tests for the pure model-inventory presentation adapter."""
from __future__ import annotations

from sonder_runtime.adapters.model_inventory_formatting import (
    inventory_model_name,
    inventory_model_names,
    residency_display,
)


def test_inventory_names_use_name_then_model_and_casefold_deduplicate():
    assert inventory_model_name({"model": "fallback:latest"}) == "fallback:latest"
    assert inventory_model_names([
        {"name": "Zeta:latest"},
        {"model": "alpha:latest"},
        {"name": "zETA:latest"},
        {},
    ]) == ["alpha:latest", "Zeta:latest"]


def test_residency_display_preserves_cpu_and_gpu_indicators():
    gib = 2**30
    assert residency_display({"name": "cpu", "size": 3 * gib, "size_vram": 0}) == (
        "cpu (3.0 GiB, CPU only)"
    )
    assert residency_display({"name": "split", "size": 4 * gib, "size_vram": 2 * gib}) == (
        "split (4.0 GiB, 50% GPU)"
    )


def test_residency_display_degrades_malformed_metadata_and_clamps_vram():
    assert residency_display({"name": "unknown", "size": "4 GiB"}) == "unknown"
    assert residency_display({"name": "over", "size": 1 * 2**30, "size_vram": 2 * 2**30}) == (
        "over (1.0 GiB, 100% GPU)"
    )
