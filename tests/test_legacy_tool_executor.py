"""SPEC-3: LegacyToolExecutor over guarded workbench / file_ops primitives."""
from __future__ import annotations

import json
import shutil

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import pytest

from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter as LegacyToolExecutor
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    # Root the guarded file tools at the temp workspace.
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    return LegacyToolExecutor()


def _ctx():
    return local_owner_context(correlation_id="req_tool")


def test_canonical_adapter_owns_legacy_compatibility_identity():
    assert LegacyToolExecutor is ToolExecutorAdapter


class _Cancelled:
    cancelled = True

    def wait(self, timeout=None):
        return True


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


def test_read_only_workbench_tools_use_packaged_guards(executor, tmp_path):
    (tmp_path / "notes.txt").write_text("needle\nsecond line\n", encoding="utf-8")
    (tmp_path / "script.py").write_text("print('ok')\n", encoding="utf-8")

    found = executor.execute(ToolCall("file_find", {"query": "notes.txt"}), _ctx())
    ranged = executor.execute(
        ToolCall("file_read_range", {"path": "notes.txt", "start_line": 2, "end_line": 2}),
        _ctx(),
    )
    searched = executor.execute(
        ToolCall("text_search", {"query": "needle", "root": "."}), _ctx()
    )
    scripts = executor.execute(
        ToolCall("script_search", {"query": "script.py", "root": "."}), _ctx()
    )

    assert found.ok and "notes.txt" in found.output
    assert ranged.ok and "second line" in ranged.output
    assert searched.ok and "needle" in searched.output
    assert scripts.ok and "script.py" in scripts.output


def test_copy_move_batch_write_and_delete_preserve_guarded_contract(executor, tmp_path):
    (tmp_path / "source.txt").write_text("copy me", encoding="utf-8")
    copied = executor.execute(
        ToolCall("file_copy", {"source": "source.txt", "destination": "copy.txt"}), _ctx()
    )
    moved = executor.execute(
        ToolCall("file_move", {"source": "copy.txt", "destination": "moved.txt"}), _ctx()
    )
    batch = executor.execute(
        ToolCall("file_batch_write", {
            "operations": [{"path": "batch.txt", "content": "batch", "mode": "create"}],
        }), _ctx()
    )
    preview = executor.execute(
        ToolCall("file_delete", {"path": "moved.txt", "dry_run": True}), _ctx()
    )

    assert copied.ok
    assert moved.ok and not (tmp_path / "copy.txt").exists()
    assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "copy me"
    assert batch.ok and (tmp_path / "batch.txt").read_text(encoding="utf-8") == "batch"
    assert preview.ok and preview.evidence["dry_run"] is True
    assert (tmp_path / "moved.txt").exists()


def test_json_and_unified_text_patch_tools_preserve_transaction_reports(executor, tmp_path):
    target = tmp_path / "data.json"
    target.write_text('{"value": 1}\n', encoding="utf-8")
    json_result = executor.execute(
        ToolCall("json_patch", {
            "path": "data.json",
            "operations_json": '[{"op":"replace","path":"/value","value":2}]',
            "mode": "apply",
        }), _ctx()
    )
    patch_result = executor.execute(
        ToolCall("text_patch", {
            "root": ".",
            "patch": "--- a/data.json\n+++ b/data.json\n@@ -1,3 +1,3 @@\n {\n-  \"value\": 2\n+  \"value\": 3\n }\n",
            "apply": True,
        }), _ctx()
    )

    assert json_result.ok and json.loads(json_result.output)["applied"] is True
    assert patch_result.ok and json.loads(patch_result.output)["applied"] is True
    assert json.loads(target.read_text(encoding="utf-8"))["value"] == 3


def test_image_inspection_uses_bounded_packaged_workbench(executor, tmp_path):
    image = tmp_path / "sample.svg"
    image.write_text(
        '<svg width="12" height="8" xmlns="http://www.w3.org/2000/svg"></svg>',
        encoding="utf-8",
    )
    result = executor.execute(ToolCall("image_inspect", {"path": "sample.svg"}), _ctx())
    assert result.ok
    assert result.evidence["format"] == "SVG"
    assert result.evidence["width"] == 12
    assert result.evidence["height"] == 8


def test_process_inventory_preserves_explicit_opt_in_boundary(executor, monkeypatch):
    monkeypatch.delenv("SONDER_PROCESS_INSPECTION", raising=False)
    result = executor.execute(ToolCall("process_list", {}), _ctx())
    assert result.ok is False
    assert result.evidence["status"] == "opt_in_required"


def test_artifact_risk_uses_packaged_static_inspector(executor, tmp_path):
    artifact = tmp_path / "sample.txt"
    artifact.write_text("ordinary local text\n", encoding="utf-8")
    result = executor.execute(
        ToolCall("artifact_risk_inspect", {"path": "sample.txt"}), _ctx()
    )
    assert result.ok
    assert result.evidence["kind"] == "binary"


def test_guard_rejection_surfaces_as_not_ok(executor):
    # Escaping the workspace root must fail closed as ok=False, not raise.
    result = executor.execute(
        ToolCall("read_file", {"path": "/etc/passwd"}), _ctx()
    )
    assert result.ok is False
    assert result.error_code  # a captured error type, not an exception


def test_cancelled_context_cannot_mutate_files(executor, tmp_path):
    context = local_owner_context(
        correlation_id="req_cancelled", cancellation=_Cancelled()
    )
    result = executor.execute(
        ToolCall("write_file", {"path": "cancelled.txt", "content": "no"}),
        context,
    )
    assert result.ok is False
    assert result.error_code == "Cancelled"
    assert not (tmp_path / "cancelled.txt").exists()


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
    assert isinstance(app.tool_executor, ToolExecutorAdapter)
    bootstrap_app.reset_for_tests()
