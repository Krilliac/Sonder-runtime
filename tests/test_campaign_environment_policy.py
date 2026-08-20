from __future__ import annotations

import server
from sonder_runtime.domain.campaign_environment import environment_failure


def test_environment_failure_preserves_host_toolchain_sentinel():
    assert environment_failure("missing runtime/compiler: node") is True
    assert environment_failure("missing runtime/compiler: csc") is True


def test_environment_failure_rejects_model_and_runtime_failures():
    for output in (
        "wrong output; expected exactly '42', got '41'",
        "no python code block returned",
        "(timed out after 8s)",
        "",
        None,
    ):
        assert environment_failure(output) is False


def test_server_alias_preserves_identity():
    assert server._campaign_environment_failure is environment_failure
