import pytest
import server

from sonder_runtime.domain.fanout_policy import (
    declares_generative_capability,
    nonchat_reason,
)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"capabilities": ["chat"]}, ""),
        ({"capabilities": ["embedding"]}, "embedding-only capability"),
        ({"details": {"capabilities": ["vision"]}}, "vision-only capability"),
        ({"details": {"family": "LLaVA"}}, "known vision-only model family"),
        ({"name": "llava:latest"}, "known vision-only model family"),
        ({"name": "custom-text:latest"}, ""),
        (None, ""),
    ],
)
def test_nonchat_reason_classifies_catalog_records(record, expected):
    assert nonchat_reason(record) == expected


def test_explicit_generative_capability_wins_over_vision_metadata():
    assert nonchat_reason(
        {"capabilities": ["chat", "vision"], "details": {"family": "llava"}}
    ) == ""


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"capabilities": ["chat"]}, True),
        ({"details": {"capabilities": ["completion"]}}, True),
        ({"capabilities": ["embedding", "vision"]}, False),
        ({"name": "unknown:latest"}, False),
        (None, False),
    ],
)
def test_declares_generative_capability_requires_explicit_catalog_metadata(record, expected):
    assert declares_generative_capability(record) is expected


def test_server_keeps_identity_compatible_generative_capability_alias():
    assert server._fanout_declares_generative_capability is declares_generative_capability
