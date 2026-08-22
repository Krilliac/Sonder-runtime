import pytest

import server
from sonder_runtime.domain import model_routing


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("kimi-k3:cloud", True),
        ("qwen3-coder:480b-cloud", True),
        ("KIMI-K3:CLOUD", True),
        ("local-model:latest", False),
        ("cloud-code", False),
        ("", False),
        (None, False),
    ],
)
def test_cloud_model_classifier_preserves_name_contract(model, expected):
    assert model_routing.is_cloud_model_name(model) is expected


def test_server_keeps_identity_compatible_classifier_alias():
    assert server._is_cloud_model_name is model_routing.is_cloud_model_name
