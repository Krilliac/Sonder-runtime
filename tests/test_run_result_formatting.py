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
        'workspace run\n'
        '  command: ["python", "-m", "pytest"]\n'
        '  cwd: C:/repo\n'
        '  ok: False\n'
        '  returncode: 1\n'
        '  timed_out: False\n'
        '  elapsed_ms: 42\n'
        'stdout:\n'
        'one\n'
        'stderr:\n'
        'boom'
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
