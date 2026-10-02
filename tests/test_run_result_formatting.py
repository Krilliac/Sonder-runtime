import ast
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.observability.run_result_formatting import format_run_result


def test_format_run_result_preserves_command_metadata_and_streams():
    rendered = format_run_result(
        "workspace run",
        {
            "command": ["python", "-m", "pytest"],
            "cwd": "C:/repo",
            "ok": False,
            "returncode": 1,
            "timed_out": False,
            "elapsed_ms": 42,
            "stdout": "one\n\n",
            "stderr": "boom\n\n",
        },
    )

    assert rendered == (
        'exit 1 (failed, 0.042 s)\n'
        'workspace run\n'
        '  command: ["python", "-m", "pytest"]\n'
        '  cwd: C:/repo\n'
        '  ok: False\n'
        '  returncode: 1\n'
        '  timed_out: False\n'
        '  elapsed_ms: 42\n'
        'stdout:\n'
        'one\n\n\n'
        'stderr:\n'
        'boom\n\n'
    )


def test_format_run_result_reports_pre_spawn_error_before_output():
    rendered = format_run_result(
        "test run",
        {"ok": False, "error": "unknown framework", "stdout": "child"},
    )

    assert rendered.index("  error: unknown framework") < rendered.index("stdout:")


def test_format_run_result_marks_truncated_streams():
    rendered = format_run_result(
        "lint",
        {"stdout_truncated": True, "stderr_truncated": False},
    )

    assert rendered.endswith("  output truncated: true")


def _result(stdout="", stderr="", **fields):
    return dict(command=["python", "check.py"], cwd="", ok=False, returncode=1,
                timed_out=False, elapsed_ms=42, stdout=stdout, stderr=stderr, **fields)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_large_output_keeps_verdict_failure_and_summary_before_streams(stream):
    output = "x" * 300_000 + "\nFAILED tests/x.py::t - AssertionError\n1 failed\n"
    rendered = format_run_result("workspace run", _result(**{stream: output}), digest=True)
    assert rendered.splitlines()[0] == "exit 1 (failed, 0.042 s)"
    assert len(rendered) <= 6000
    digest = rendered.split("digest:\n", 1)[1].split(stream + ":", 1)[0]
    assert "FAILED tests/x.py::t - AssertionError" in digest
    assert "1 failed" in digest
    assert "x" * 1500 in rendered
    assert output[-2500:] in rendered


@pytest.mark.parametrize("size", [200, 4999, 5000])
def test_small_combined_output_is_shown_whole(size):
    output = " \n" + "x" * (size - 4) + "\n "
    rendered = format_run_result("workspace run", _result(stdout=output), digest=True)
    assert "stdout:\n" + output in rendered
    assert len(rendered) <= 6000
    assert "full output:" not in rendered


def test_digest_precedes_both_streams_and_limits_failures_and_locations():
    output = "\n".join("FAILED tests/x.py::t%d - assertion" % i for i in range(40))
    output += "\n" + "\n".join("src/x.py:%d: error: broken" % i for i in range(1, 30))
    output += "\n40 failed in 0.10s\n"
    rendered = format_run_result("test run", _result(output, "stderr sentinel"), digest=True)
    block = rendered.split("digest:\n")[1].split("stdout:")[0]
    assert block.splitlines()[0] == "  summary: 40 failed in 0.10s"
    assert 0 < block.count("FAILED ") <= 20
    assert 0 < block.count("src/x.py:") <= 10
    assert rendered.index("digest:") < rendered.index("stdout:") < rendered.index("stderr:")


@pytest.mark.parametrize("digest", [False, True])
def test_large_metadata_cannot_displace_output_tail(digest):
    data = _result("head\n" + "x" * 10_000 + "\nEND", "1 failed\n")
    data.update(command=["a" * 20_000], error="e" * 20_000,
                guard="busy", holder="h" * 20_000, recovery="r" * 20_000)
    rendered = format_run_result("t" * 20_000, data, digest=digest)
    assert len(rendered) <= 6000
    assert "END" in rendered and "1 failed" in rendered
    for field in ("command:", "error:", "guard:", "holder:", "recovery:"):
        assert field in rendered


@pytest.mark.parametrize("filler", [0, 4990, 300_000])
@pytest.mark.parametrize("report_chars", [40, 1200, 2400])
def test_context_directly_follows_the_exit_line_whole(filler, report_chars):
    # script_run's artifact-risk report is assessed before the run: nothing the
    # run produced may precede it, and output pressure may not clip it.
    context = "artifact risk: {%s}\nexecution allowed by effective policy report" % ("r" * report_chars)
    stdout = "x" * filler + "\nFAILED tests/x.py::t - AssertionError\n1 failed\n"
    rendered = format_run_result("script run", _result(stdout), digest=True, context=context)
    assert rendered.startswith("exit 1 (failed, 0.042 s)\n" + context + "\nscript run\n")
    assert len(rendered) <= 6000
    assert stdout[-1000:] in rendered
    digest = rendered.split("\ndigest:\n", 1)[1].split("\nstdout:\n", 1)[0]
    assert "FAILED tests/x.py::t - AssertionError" in digest and "1 failed" in digest
    for field in ("command", "cwd", "ok", "returncode", "timed_out", "elapsed_ms"):
        assert "\n  %s: " % field in rendered


@pytest.mark.parametrize("fields, expected", [
    ({"ok": True, "returncode": 0}, "exit 0 (ok, 0.042 s)"),
    ({"timed_out": True, "returncode": None}, "exit None (timed_out, 0.042 s)"),
    ({"error": "unavailable", "returncode": None}, "exit None (error, 0.042 s)"),
])
def test_exit_line_uses_process_status(fields, expected):
    data = _result()
    data.update(fields)
    assert format_run_result("run", data).splitlines()[0] == expected


def test_complete_capture_is_saved_inside_sonder_with_byte_count(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    stdout, stderr = "\u03bb" * 5001, "1 failed\n"
    data = _result(stdout, stderr)
    data["cwd"] = str(tmp_path)
    rendered = format_run_result("run", data, digest=True)
    logs = list((tmp_path / ".sonder" / "run").glob("*.log"))
    assert len(logs) == 1
    payload = (stdout + "\n" + stderr).encode("utf-8")
    assert logs[0].read_bytes() == payload
    assert "full output: %s (%d bytes); page it with file_read offset=" % (logs[0], len(payload)) in rendered
    assert len(rendered) <= 6000


def test_log_guard_failure_preserves_verdict_and_never_claims_a_log(tmp_path, monkeypatch):
    def deny(path, **kwargs):
        raise PermissionError("denied by path guard")

    monkeypatch.setattr(file_ops, "resolve_mutation_path", deny)
    data = _result("x" * 6000 + "\n1 failed\n")
    data["cwd"] = str(tmp_path)
    rendered = format_run_result("run", data, digest=True)
    assert rendered.startswith("exit 1 (")
    assert "full output: unavailable (denied by path guard)" in rendered
    assert "1 failed" in rendered
    assert not (tmp_path / ".sonder").exists()


def test_incomplete_capture_does_not_masquerade_as_full_output(tmp_path):
    data = _result("x" * 6000, stdout_truncated=True)
    data["cwd"] = str(tmp_path)
    rendered = format_run_result("run", data, digest=True)
    assert "full output: unavailable" in rendered
    assert not (tmp_path / ".sonder").exists()


def test_code_run_keeps_metadata_and_input_recovery_advice():
    data = _result("enter guess", "EOFError", language="cpp", timeout=10)
    data.update(error="timed out after 10s", returncode=None)
    rendered = format_run_result("code run", data, digest=True)
    assert "language: cpp" in rendered
    assert "timeout: 10" in rendered
    assert "non-interactive" in rendered
    assert "provide stdin" in rendered
    assert "bounded smoke test" in rendered


def test_noisy_stderr_cannot_hide_stdout_pytest_summary():
    data = _result("x" * 20_000 + "\n1 failed in 0.10s\n", "noise\n" * 5000)
    rendered = format_run_result("test run", data, digest=True)
    assert "  summary: 1 failed in 0.10s" in rendered
    assert len(rendered) <= 6000


def test_supplied_log_path_is_guarded_and_bytes_are_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    directory = tmp_path / ".sonder" / "run"
    directory.mkdir(parents=True)
    log = directory / "existing.log"
    log.write_bytes(b"complete log")
    data = _result("x" * 6000, output_log=str(log), output_bytes=999)
    data["cwd"] = str(tmp_path)
    rendered = format_run_result("run", data, digest=True)
    assert "(%d bytes)" % len(b"complete log") in rendered
    data["output_log"] = str(tmp_path / "outside.log")
    assert "full output: unavailable" in format_run_result("run", data, digest=True)


def _host_function(name, data):
    """Exercise the real host body without importing its live service graph."""
    source = Path(__file__).resolve().parents[1] / "server.py"
    node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    scope = {
        "time": time, "_maybe_live_reload": lambda: None,
        "_record_direct_tool": lambda *args, **kwargs: None,
        "_file_bypass_allowed": lambda *args: False,
        "_format_run_result": format_run_result,
        "workbench": SimpleNamespace(run_program=lambda *a, **k: data),
        "code_runner": SimpleNamespace(run_code=lambda **k: data),
        "artifact_risk_module": SimpleNamespace(
            run_script_under_policy=lambda *a, **k: ({"policy": "off"}, data),
            format_result=lambda risk: "off", ArtifactRiskDenied=RuntimeError,
        ),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), scope)
    return scope[name]


@pytest.mark.parametrize("name,args", [
    ("workspace_run", {"program": "python"}),
    ("script_run", {"path": "test.py"}),
    ("run_code", {"code": "print('test')"}),
])
def test_host_tools_enable_digest_and_bound_the_entire_observation(name, args):
    data = _result("x" * 300_000 + "\nFAILED tests/x.py::t - AssertionError\n1 failed\n")
    rendered = _host_function(name, data)(**args)
    assert rendered.splitlines()[0].startswith("exit 1 (")
    assert len(rendered) <= 6000
    assert rendered.index("digest:") < rendered.index("stdout:")
    assert "FAILED tests/x.py::t - AssertionError" in rendered
    assert "1 failed" in rendered
    if name == "script_run":
        assert "artifact risk: off\nexecution allowed by effective policy off" in rendered
