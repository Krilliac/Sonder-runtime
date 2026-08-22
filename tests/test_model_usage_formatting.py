import pytest

import server
from sonder_runtime.domain.model_usage_formatting import usage_source


def test_server_keeps_identity_compatible_usage_source_alias():
    assert server._model_usage_source is usage_source


@pytest.mark.parametrize(
    ("tokens_in", "tokens_out", "expected"),
    [
        (12, 34, "ollama"),
        (None, None, "estimated"),
        (12, None, "mixed"),
        (None, 34, "mixed"),
    ],
)
def test_usage_source_classifies_provider_count_completeness(
    tokens_in, tokens_out, expected
):
    assert usage_source(tokens_in, tokens_out) == expected
