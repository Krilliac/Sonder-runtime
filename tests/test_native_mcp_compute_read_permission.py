"""Native MCP compute reads go through the runtime permission decision.

``compute_submit`` and ``compute_cancel`` were gated, but ``compute_status``
and ``compute_artifact_fetch`` went straight to the compute service, so an
operator deny rule (or ``plan``/unclassified refusal) never applied to a
read that returns private job artifact bytes.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from sonder_runtime.bootstrap.native_mcp import run_native_mcp

from tests.test_native_mcp import _app


def _call(app, name, arguments):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2.0", "capabilities": {"tools": {}},
        }},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": name, "arguments": arguments,
        }},
    ]
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"),
        output_stream=output,
    )
    return [json.loads(line) for line in output.getvalue().splitlines()][1]["result"]


def _never(*_args, **_kwargs):
    raise AssertionError("denied compute read must not reach the compute service")


@pytest.mark.parametrize("name,arguments", [
    ("compute_status", {"controller_job_id": "controller-1"}),
    ("compute_artifact_fetch", {"controller_job_id": "controller-1", "name": "out.txt"}),
])
def test_denied_compute_reads_do_not_execute(monkeypatch, name, arguments):
    from sonder_runtime.adapters.security.permission_policy import permission_policy

    seen = []

    def deny(tool, **kwargs):
        seen.append((tool, kwargs))
        return SimpleNamespace(action="deny")

    monkeypatch.setattr(permission_policy, "decide_for_caller", deny)
    app = _app()
    app.compute_service = lambda: SimpleNamespace(
        status=_never, artifact=_never, fetch_artifact=_never, artifact_fetch=_never,
    )
    result = _call(app, name, arguments)
    assert result["isError"] is True
    assert result["error"] == "permission_denied"
    assert seen and seen[0][0] == name
    assert seen[0][1]["surface"] == "native-mcp"


def test_compute_reads_have_a_deliberate_permission_class():
    import permission_modes

    assert permission_modes.risk_of("compute_status") == "safe"
    assert permission_modes.risk_of("compute_artifact_fetch") == "safe"
