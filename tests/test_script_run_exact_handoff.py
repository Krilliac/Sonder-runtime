"""script_run exact handoff: enforcing policies run the sealed inspected bytes.

On Linux, ``deny-*`` policies copy the script through the guarded no-follow
handle into a sealed memfd, inspect that copy, and execute the same
descriptor. These tests replace the on-disk script between the scan and the
launch and prove the inspected bytes still run.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

import server
import sonder_runtime.adapters.artifact_risk as artifact_risk
import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the sealed-memfd exact handoff is Linux-only",
)


@pytest.fixture()
def root(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: project)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "deny-high")
    return project


def _swap_before_launch(monkeypatch, swap):
    """Mutate the on-disk script after the scan, immediately before launch."""
    real_run_script = workbench.run_script
    seen = []

    def swapping_run_script(*args, **kwargs):
        seen.append(kwargs.get("sealed_script"))
        swap()
        return real_run_script(*args, **kwargs)

    monkeypatch.setattr(server.workbench, "run_script", swapping_run_script)
    return seen


def test_benign_python_runs_with_script_semantics(root):
    (root / "helper_mod.py").write_text("VALUE = 'sibling-import'\n", encoding="utf-8")
    script = root / "main.py"
    script.write_text(
        "import os, sys\n"
        "import helper_mod\n"
        "print('file=' + __file__)\n"
        "print('argv=' + '|'.join(sys.argv))\n"
        "print('path0=' + sys.path[0])\n"
        "print('cwd=' + os.getcwd())\n"
        "print('name=' + __name__)\n"
        "print(helper_mod.VALUE)\n"
        "print('stdin=' + sys.stdin.read())\n",
        encoding="utf-8",
    )

    output = server.script_run(str(script), args_json='["a", "b c"]', stdin="fed")

    assert "execution allowed by effective policy deny-high" in output
    assert '"exact_handoff":"linux-memfd-sealed"' in output
    assert "returncode: 0" in output
    assert "file=%s" % script in output
    assert "argv=%s|a|b c" % script in output
    assert "path0=%s" % root in output
    assert "cwd=%s" % root in output
    assert "name=__main__" in output
    assert "sibling-import" in output
    assert "stdin=fed" in output


def test_in_place_rewrite_after_scan_still_runs_inspected_bytes(root, monkeypatch):
    script = root / "job.py"
    script.write_text("print('INSPECTED')\n", encoding="utf-8")
    replacement = "print('SWAPPED')\n"

    def rewrite():
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(replacement)

    seen = _swap_before_launch(monkeypatch, rewrite)
    output = server.script_run(str(script))

    assert seen and seen[0] is not None
    assert "INSPECTED" in output
    assert "SWAPPED" not in output
    assert script.read_text(encoding="utf-8") == replacement


def test_rename_replacement_with_high_risk_payload_after_scan_is_not_run(
    root, monkeypatch, tmp_path
):
    marker = tmp_path / "payload-ran"
    script = root / "job.py"
    script.write_text("print('INSPECTED')\n", encoding="utf-8")
    staged = root / "staged.py"
    staged.write_text(
        "# powershell -EncodedCommand AAAA\n"
        "open(%r, 'w').write('ran')\n" % str(marker),
        encoding="utf-8",
    )

    _swap_before_launch(monkeypatch, lambda: os.replace(staged, script))
    output = server.script_run(str(script))

    assert "INSPECTED" in output
    assert not marker.exists()


def test_high_risk_python_is_denied_before_launch(root, monkeypatch, tmp_path):
    marker = tmp_path / "payload-ran"
    script = root / "bad.py"
    script.write_text(
        "# powershell -EncodedCommand AAAA\n"
        "open(%r, 'w').write('ran')\n" % str(marker),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(server.workbench, "run_script", lambda *a, **k: calls.append(k))

    output = server.script_run(str(script))

    assert '"denied":true' in output
    assert '"risk":"high"' in output
    assert "execution denied by effective policy deny-high" in output
    assert calls == []
    assert not marker.exists()


def test_uncaught_exception_reports_script_path(root):
    script = root / "boom.py"
    script.write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    output = server.script_run(str(script))

    assert "returncode: 1" in output
    assert 'File "%s", line 1' % script in output
    assert "RuntimeError: boom" in output


def test_shell_script_runs_sealed_copy(root, monkeypatch):
    if not workbench.runtime_paths.bash_executable():
        pytest.skip("bash is not installed")
    script = root / "job.sh"
    script.write_text('echo "INSPECTED $1"\necho "zero=$0"\n', encoding="utf-8")

    def rewrite():
        script.write_text('echo "SWAPPED"\n', encoding="utf-8")

    _swap_before_launch(monkeypatch, rewrite)
    output = server.script_run(str(script), args_json='["arg"]')

    assert "INSPECTED arg" in output
    assert "SWAPPED" not in output
    # Characterized difference: bash sees the sealed descriptor path as $0.
    assert "zero=/proc/self/fd/" in output


def test_sealed_copy_rejects_writes_and_is_closed_after_use(root):
    script = root / "job.py"
    script.write_text("print('x')\n", encoding="utf-8")

    with artifact_risk.sealed_script_execution(str(script), requested="deny-high") as (
        risk,
        sealed,
    ):
        assert risk["risk"] == "none_detected"
        assert risk["scan_complete"] is True
        assert sealed.path == script
        assert sealed.size == len("print('x')\n")
        with pytest.raises(OSError) as caught:
            os.write(sealed.fd, b"y")
        assert caught.value.errno == errno.EPERM
        with pytest.raises(OSError):
            os.ftruncate(sealed.fd, 0)
        descriptor = sealed.fd

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_sealed_script_must_match_requested_path(root):
    script = root / "job.py"
    script.write_text("print('x')\n", encoding="utf-8")
    other = root / "other.py"
    other.write_text("print('other')\n", encoding="utf-8")

    with artifact_risk.sealed_script_execution(str(script), requested="deny-high") as (
        _risk,
        sealed,
    ):
        with pytest.raises(PermissionError, match="does not match"):
            workbench.run_script(str(other), sealed_script=sealed)


def test_script_over_scan_budget_is_denied(root, monkeypatch):
    script = root / "big.py"
    script.write_text("print('x')\n" * 200, encoding="utf-8")
    monkeypatch.setattr(artifact_risk, "MAX_SCAN_BYTES", 1024)
    calls = []
    monkeypatch.setattr(server.workbench, "run_script", lambda *a, **k: calls.append(k))

    output = server.script_run(str(script))

    assert "exact_handoff_exceeds_scan_budget" in output
    assert calls == []


def test_in_root_alias_seals_and_reports_the_canonical_script(root):
    # Both the guarded open and run_script resolve an in-root alias to its
    # canonical target before any bytes are read; the sealed copy and the
    # script identity the child sees are that target, never the link name.
    target = root / "real.py"
    target.write_text("print('file=' + __file__)\n", encoding="utf-8")
    link = root / "alias.py"
    link.symlink_to(target)

    output = server.script_run(str(link))

    assert '"exact_handoff":"linux-memfd-sealed"' in output
    assert "file=%s" % target in output


def test_report_policy_keeps_path_launch(root, monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "report")
    script = root / "job.py"
    script.write_text("print(__file__)\n", encoding="utf-8")

    output = server.script_run(str(script))

    assert "execution allowed by effective policy report" in output
    assert "exact_handoff" not in output
    assert str(script) in output


def test_bootstrap_lives_beside_workbench():
    assert Path(workbench._SEALED_PYTHON_MAIN).is_file()


# --- the native run_script tool goes through the same gate ---------------------


def _native_run_script(root, **arguments):
    from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.ports.tool_executor import ToolCall

    context = local_owner_context(correlation_id="native-run-script", workspace_roots=(root,))
    return ToolExecutorAdapter().execute(ToolCall("run_script", arguments), context)


def test_native_run_script_denies_high_risk_under_deny_high(root, tmp_path):
    marker = tmp_path / "payload-ran"
    script = root / "bad.py"
    script.write_text(
        "# powershell -EncodedCommand AAAA\n"
        "open(%r, 'w').write('ran')\n" % str(marker),
        encoding="utf-8",
    )

    result = _native_run_script(root, path=str(script))

    assert result.ok is False
    assert result.error_code == "ArtifactRiskDenied"
    assert "execution denied by effective policy deny-high" in result.output
    assert result.evidence["artifact_risk"]["risk"] == "high"
    assert not marker.exists()


def test_native_run_script_runs_the_sealed_copy_under_deny_high(root, monkeypatch):
    script = root / "job.py"
    script.write_text("print('inspected')\n", encoding="utf-8")
    _swap_before_launch(
        monkeypatch,
        lambda: script.write_text("print('swapped')\n", encoding="utf-8"),
    )

    result = _native_run_script(root, path=str(script))

    assert result.ok is True
    assert result.output.strip() == "inspected"
    assert result.evidence["returncode"] == 0
    assert result.evidence["exact_handoff"]["mechanism"] == "linux-memfd-sealed"
    assert result.evidence["artifact_risk"]["policy"] == "deny-high"


def test_native_run_script_refuses_runner_without_exact_handoff(root):
    script = root / "job.rb"
    script.write_text("puts 'hi'\n", encoding="utf-8")

    result = _native_run_script(root, path=str(script))

    assert result.ok is False
    assert result.error_code == "ArtifactRiskDenied"
    assert result.evidence["artifact_risk"]["denial_reason"] == (
        artifact_risk.EXACT_HANDOFF_UNAVAILABLE
    )


def test_native_run_script_reports_risk_under_report_policy(root, monkeypatch):
    monkeypatch.setenv("SONDER_EXECUTION_RISK_POLICY", "report")
    script = root / "job.py"
    script.write_text("print('ran')\n", encoding="utf-8")

    result = _native_run_script(root, path=str(script))

    assert result.ok is True
    assert result.output.strip() == "ran"
    assert "exact_handoff" not in result.evidence
    assert result.evidence["artifact_risk"]["policy"] == "report"
