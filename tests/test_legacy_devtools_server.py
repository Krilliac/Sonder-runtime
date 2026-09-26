"""Legacy MCP ``tool_inventory`` / ``output_digest`` and the digest on run tools."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import permission_modes as pm
import server
import tool_capabilities as capabilities
from sonder_runtime.application.diagnostics.ports import TextWindow
from sonder_runtime.application.diagnostics.service import OutputDigestService
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.platform.logging import Redactor


WIRE = {"object": "tool_inventory", "tools": [{"name": "gcc", "path": "~/bin/gcc"}]}


@pytest.fixture
def view_to_wire(monkeypatch):
    """Record what reaches the host-tools serializer and answer a fixed wire."""
    from sonder_runtime.domain.host_tools import model

    calls = []

    def fake(view):
        calls.append(view)
        return dict(WIRE, filtered_by=view.filtered_by)

    monkeypatch.setattr(model, "view_to_wire", fake)
    return calls


class _Inventory:
    def __init__(self):
        self.calls = []

    def view(self, *, category=None, name=None, refresh=False, redacted=True):
        self.calls.append((category, name, refresh, redacted))
        if category == "bogus":
            raise InvalidInput("unknown tool category")
        return SimpleNamespace(filtered_by=category or name or "")


class _Jobs:
    def job_metadata(self, job_id):
        return {"kind": "tool.test_run", "principal_id": "owner"} if job_id == "test-run-1" else None

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        text = "FAILED t.py::a - boom\n1 failed in 0.1s\n"
        return TextWindow(text, "job:" + job_id, len(text), len(text), False)


class _Files:
    def read_file_window(self, path, **kwargs):
        raise PermissionError("DIGEST_SOURCE_REJECTED")


@pytest.fixture
def composed(monkeypatch):
    services = SimpleNamespace(
        inventory=_Inventory(),
        test_runs=None,
        digest=OutputDigestService(_Files(), _Jobs(), redact=Redactor(env={}).redact),
    )
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace(developer_tools=services))
    return services


def test_tool_inventory_returns_redacted_json(composed, view_to_wire):
    payload = json.loads(server.tool_inventory(category="compiler"))
    assert payload["ok"] is True and payload["object"] == "tool_inventory"
    assert payload["filtered_by"] == "compiler"
    assert composed.inventory.calls == [("compiler", None, False, True)]
    assert "ERROR:" not in server.tool_inventory(category="bogus")
    refused = json.loads(server.tool_inventory(category="bogus"))
    assert refused == {"ok": False, "error_code": "INVALID_INVENTORY_QUERY",
                       "detail": "unknown tool category"}


def test_output_digest_job_path_and_argument_errors(composed):
    ok = json.loads(server.output_digest(job_id="test-run-1"))
    assert ok["ok"] is True and ok["digest"]["summary"]["failed"] == 1
    assert json.loads(server.output_digest(job_id="nope")) == {"ok": False, "error_code": "JOB_NOT_FOUND"}
    assert json.loads(server.output_digest(path="x.log")) == {
        "ok": False, "error_code": "DIGEST_SOURCE_REJECTED",
    }
    both = json.loads(server.output_digest(path="x.log", job_id="test-run-1"))
    assert both["error_code"] == "INVALID_DIGEST_REQUEST"
    neither = json.loads(server.output_digest())
    assert neither["error_code"] == "INVALID_DIGEST_REQUEST"


def test_uncomposed_runtime_answers_unavailable(monkeypatch):
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace())
    for output in (server.tool_inventory(), server.output_digest(job_id="x")):
        assert json.loads(output) == {"ok": False, "error_code": "DEVELOPER_TOOLS_UNAVAILABLE"}
        assert not output.startswith("ERROR:")


@pytest.fixture
def _dispatch_allowed(unattended_effects_allowed, every_tool_allowed_by_rule):
    return None


def test_both_are_dispatchable_to_agents(monkeypatch, _dispatch_allowed):
    assert {"tool_inventory", "output_digest"} <= capabilities.dispatch_names(server._agent_dispatch)
    calls = []
    monkeypatch.setattr(server, "tool_inventory", lambda **k: calls.append(("inv", k)) or "{}")
    monkeypatch.setattr(server, "output_digest", lambda **k: calls.append(("dig", k)) or "{}")
    server._agent_dispatch("tool_inventory", {"category": "compiler"})
    server._agent_dispatch("output_digest", {"job_id": "test-run-1"})
    assert calls == [
        ("inv", {"category": "compiler", "name": "", "refresh": False}),
        ("dig", {"path": "", "job_id": "test-run-1", "tail_lines": 20}),
    ]


def test_policy_sets_read_only_and_local_only():
    for name in ("tool_inventory", "output_digest"):
        assert name in server.REPOSITORY_READ_ONLY_TOOLS
        assert name in server._CLOUD_AGENT_LOCAL_ONLY_TOOLS
        assert name in server._WORK_INSPECTION_TOOLS
        assert pm.risk_of(name) == "safe"
    assert "output_digest" in server._PROJECT_SCOPED_PATH_TOOLS


def test_project_scope_keeps_an_omitted_digest_path_omitted(tmp_path):
    scoped = server._project_scope_args("output_digest", {"job_id": "test-run-1"}, str(tmp_path))
    assert not scoped.get("path")
    rebased = server._project_scope_args("output_digest", {"path": "logs/a.log"}, str(tmp_path))
    assert rebased["path"] == str(tmp_path / "logs" / "a.log")


def test_legacy_test_run_output_carries_a_digest_block(monkeypatch):
    monkeypatch.setattr(server.harness_tools, "test_run", lambda **_k: {
        "command": ["python", "-m", "pytest"], "cwd": "/w", "ok": False, "returncode": 1,
        "timed_out": False, "elapsed_ms": 5, "framework": "pytest",
        "stdout": "FAILED t.py::a - boom\n1 failed in 0.1s\n", "stderr": "",
    })
    output = server.test_run(root=".")
    head, _, block = output.partition("\ndigest:\n")
    assert "  ok: False" in head
    assert block.startswith("  summary: 1 failed in 0.1s")
    assert "    FAILED t.py::a - boom" in block
