import pytest

from sonder_runtime.domain.fanout_policy import nonchat_reason


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
