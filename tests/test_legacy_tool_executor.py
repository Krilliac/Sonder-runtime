"""SPEC-3: LegacyToolExecutor over guarded workbench / file_ops primitives."""
from __future__ import annotations

import shutil

import file_ops
import pytest

from sonder_runtime.adapters.legacy.services import LegacyToolExecutor
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    # Root the guarded file tools at the temp workspace.
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: tmp_path / "home")
    return LegacyToolExecutor()


def _ctx():
    return local_owner_context(correlation_id="req_tool")


def test_unknown_tool_fails_closed(executor):
    result = executor.execute(ToolCall("nope", {}), _ctx())
    assert result.ok is False
    assert result.error_code == "unknown_tool"


def test_write_then_read_file_round_trip(executor, tmp_path):
    w = executor.execute(
        ToolCall("write_file", {"path": "note.txt", "content": "hello sonder"}),
        _ctx(),
    )
    assert w.ok is True
    r = executor.execute(ToolCall("read_file", {"path": "note.txt"}), _ctx())
    assert r.ok is True
    assert "hello sonder" in r.output


def test_make_directory(executor, tmp_path):
    result = executor.execute(ToolCall("make_directory", {"path": "sub/dir"}), _ctx())
    assert result.ok is True
    assert (tmp_path / "sub" / "dir").is_dir()


def test_guard_rejection_surfaces_as_not_ok(executor):
    # Escaping the workspace root must fail closed as ok=False, not raise.
    result = executor.execute(
        ToolCall("read_file", {"path": "/etc/passwd"}), _ctx()
    )
    assert result.ok is False
    assert result.error_code  # a captured error type, not an exception


def test_run_program_executes_argv(executor):
    if shutil.which("python3") is None:
        pytest.skip("python3 not installed")
    result = executor.execute(
        ToolCall(
            "run_program",
            {"program": "python3", "args_json": ["-V"], "timeout": 10},
        ),
        _ctx(),
    )
    # -V prints to stdout/stderr and exits 0; the executor reports the evidence dict.
    assert "returncode" in result.evidence


def test_run_program_inline_code_is_refused(executor):
    # The C2 inline-shell guard must still fire through this port.
    if shutil.which("python3") is None:
        pytest.skip("python3 not installed")
    result = executor.execute(
        ToolCall(
            "run_program",
            {"program": "python3", "args_json": ["-c", "print(1)"], "timeout": 10},
        ),
        _ctx(),
    )
    assert result.ok is False
    assert "script_run" in result.output


def test_application_exposes_tool_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    bootstrap_app.reset_for_tests()
    app = bootstrap_app.build_application()
    assert isinstance(app.tool_executor, LegacyToolExecutor)
    bootstrap_app.reset_for_tests()
