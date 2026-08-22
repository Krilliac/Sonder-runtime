"""Focused coverage for the packaged launcher application policy seam."""
from __future__ import annotations

import pytest

import sonder_launcher
from sonder_runtime.application import lifecycle


def test_root_launcher_preserves_context_normalizer_identity_alias() -> None:
    assert sonder_launcher.normalize_context_size is lifecycle.normalize_context_size
    assert sonder_launcher.MAX_CONTEXT_TOKENS == lifecycle.MAX_CONTEXT_TOKENS


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" 32K ", "32k"), ("256.5k", "256.5k"), ("1M", "1m"), (None, "8192")],
)
def test_context_normalizer_accepts_and_canonicalizes_lifecycle_values(value, expected):
    assert lifecycle.normalize_context_size(value) == expected


@pytest.mark.parametrize("value", ["0", "1000001", "1.1", "bad", "2m"])
def test_context_normalizer_rejects_out_of_bound_or_fractional_values(value) -> None:
    with pytest.raises(ValueError):
        lifecycle.normalize_context_size(value)
