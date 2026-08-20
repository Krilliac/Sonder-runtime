import pytest

import server
from sonder_runtime.adapters import context_formatting
from sonder_runtime.adapters.observability import health_formatting as observability_health_formatting
from sonder_runtime.domain import health_formatting


@pytest.mark.parametrize(
    ("percent", "width", "expected"),
    [
        (0, 4, "[----]"),
        (0.5, 4, "[##--]"),
        (1, 4, "[####]"),
        (-1, 4, "[----]"),
        (2, 4, "[####]"),
        (None, 4, "[----]"),
    ],
)
def test_health_bar_clamps_fraction_and_preserves_width(percent, width, expected):
    assert health_formatting.health_bar(percent, width) == expected


def test_server_keeps_identity_compatible_health_bar_alias():
    assert server._health_bar is health_formatting.health_bar


def test_context_health_formatter_has_observability_ownership_and_alias():
    assert (
        context_formatting.format_context_health
        is observability_health_formatting.format_context_health
    )
