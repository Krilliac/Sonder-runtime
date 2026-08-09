"""Safety, bounds, parsing, and integration tests for log_inspect."""
import inspect
import json
import os
from pathlib import Path
import time

import pytest

import activity_tracker
import file_ops
import log_inspect
import server


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


def test_mixed_log_extracts_deterministic_clusters_repeats_and_context(project):
    path = project / "app.log"
    path.write_text(
        "2026-08-08T10:00:00Z INFO api: started\n"
        "2026-08-08T10:00:01Z [WARNING] [cache] retry 41\n"
        "context before first failure\n"
        "2026-08-08T10:00:02Z [ERROR] [worker] request 100 failed at 0xAA\n"
        "stack detail\n"
        "2026-08-08T10:00:03Z ERROR worker: request 101 failed at 0xBB\n"
        '{"timestamp":"2026-08-08T10:00:04Z","level":"error",'
        '"source":"db","message":"request 102 failed at 0xCC"}\n',
        encoding="utf-8",
    )

    first = log_inspect.inspect_log(path, context_lines=1)
    second = log_inspect.inspect_log(path, context_lines=1)

    assert log_inspect.encode_result(first) == log_inspect.encode_result(second)
    assert first["summary"]["lines_inspected"] == 7
    assert first["summary"]["error_lines"] == 3
    assert first["summary"]["warning_lines"] == 1
    assert first["timestamps"] == {
        "count": 5,
        "first": "2026-08-08T10:00:00Z",
        "last": "2026-08-08T10:00:04Z",
    }
    error_cluster = next(row for row in first["clusters"] if row["kind"] == "error")
    assert error_cluster["count"] == 3
    assert error_cluster["template"] == "request <n> failed at <hex>"
    assert error_cluster["sources"] == ["db", "worker"]
    assert first["first_failure"]["line"] == 4
    assert [row["line"] for row in first["first_failure"]["context"]] == [3, 4, 5]
    assert first["last_failure"]["line"] == 7
    assert any(row["count"] == 3 for row in first["repeated_messages"])


def test_tail_analyzes_only_last_requested_lines(project):
    path = project / "tail.log"
    path.write_text(
        "\n".join("INFO old %d" % number for number in range(20))
        + "\nERROR recent failure\nWARNING final warning\n",
        encoding="utf-8",
    )
    report = log_inspect.inspect_log(path, tail_lines=2)
    assert report["summary"]["lines_inspected"] == 2
    assert report["summary"]["error_lines"] == 1
    assert report["summary"]["warning_lines"] == 1
    assert report["first_failure"]["message"] == "recent failure"
    assert report["window"]["tail"] is True


def test_tail_reads_from_end_under_scan_cap(project):
    path = project / "large-tail.log"
    path.write_text("INFO old padding\n" * 200 + "ERROR final failure\n", encoding="utf-8")
    report = log_inspect.inspect_log(
        path, tail_lines=5, max_scan_bytes=256, max_file_bytes=100_000,
    )
    assert report["window"]["window_start_byte"] > 0
    assert report["summary"]["error_lines"] == 1
    assert report["first_failure"]["message"] == "final failure"
    assert report["scan_truncated"] is True


def test_json_log_fields_are_extracted(project):
    path = project / "json.log"
    path.write_text(
        json.dumps({
            "time": "2026-08-08T10:00:00Z", "severity": "warn",
            "logger": "queue", "msg": "backlog 12",
        }) + "\n",
        encoding="utf-8",
    )
    report = log_inspect.inspect_log(path)
    assert report["levels"] == {"WARNING": 1}
    assert report["clusters"][0]["sample"] == "backlog 12"
    assert report["sources"] == [{"source": "queue", "count": 1}]


def test_file_scan_line_result_and_output_caps_are_explicit(project):
    path = project / "caps.log"
    path.write_text(
        "\n".join("ERROR distinct-%d %s" % (number, "x" * 200) for number in range(50)),
        encoding="utf-8",
    )
    with pytest.raises(log_inspect.LogInspectError, match="file byte ceiling"):
        log_inspect.inspect_log(path, max_file_bytes=100)

    report = log_inspect.inspect_log(
        path, max_scan_bytes=1000, max_lines=3, max_line_bytes=64,
        max_results=2, max_output_bytes=1500,
    )
    assert report["summary"]["lines_inspected"] == 3
    assert report["summary"]["line_truncated"] == 3
    assert report["scan_truncated"] is True
    assert report["details_truncated"] is True
    encoded = log_inspect.encode_result(report).encode("utf-8")
    assert len(encoded) <= 1500
    assert report["output_bytes"] == len(encoded)


def test_dense_newline_input_stops_at_line_cap_without_materializing_all_lines(project):
    path = project / "dense.log"
    path.write_text("x\n" * 60_000, encoding="utf-8")
    report = log_inspect.inspect_log(
        path, max_lines=5, max_scan_bytes=200_000, max_file_bytes=200_000,
    )
    assert report["summary"]["lines_inspected"] == 5
    assert report["window"]["lines_seen"] == 6
    assert report["window"]["line_cap_truncated"] is True
    assert report["scan_truncated"] is True


def test_timeout_includes_line_parsing(project, monkeypatch):
    path = project / "slow.log"
    path.write_text("ERROR slow\n", encoding="utf-8")
    original = log_inspect._parse_line

    def slow_parse(line):
        result = original(line)
        time.sleep(0.12)
        return result

    monkeypatch.setattr(log_inspect, "_parse_line", slow_parse)
    with pytest.raises(log_inspect.LogInspectError, match="timeout ceiling"):
        log_inspect.inspect_log(path, timeout=0.05)


@pytest.mark.parametrize("payload,error", [(b"INFO ok\x00bad\n", "NUL"), (b"\xff\xfe", "UTF-8")])
def test_non_text_windows_are_rejected(project, payload, error):
    path = project / "binary.log"
    path.write_bytes(payload)
    with pytest.raises(log_inspect.LogInspectError, match=error):
        log_inspect.inspect_log(path)


def test_sensitive_control_and_outside_paths_are_rejected(project, tmp_path):
    secret = project / ".env"
    secret.write_text("ERROR secret token", encoding="utf-8")
    with pytest.raises(log_inspect.LogInspectError, match="secret|control|protected"):
        log_inspect.inspect_log(secret)

    metadata = project / ".git"
    metadata.mkdir()
    hidden = metadata / "actions.log"
    hidden.write_text("ERROR hidden", encoding="utf-8")
    with pytest.raises(log_inspect.LogInspectError, match="secret|control"):
        log_inspect.inspect_log(hidden)

    outside = tmp_path / "outside.log"
    outside.write_text("ERROR outside", encoding="utf-8")
    monkey_root = project.parent / "other"
    monkey_root.mkdir()
    escaped = monkey_root / "escaped.log"
    escaped.write_text("ERROR escaped", encoding="utf-8")
    with pytest.raises(log_inspect.LogInspectError, match="outside every authorized root"):
        log_inspect.inspect_log(escaped)


def test_symlink_and_replacement_race_are_rejected(project, tmp_path, monkeypatch):
    outside = tmp_path / "outside.log"
    outside.write_text("ERROR outside secret", encoding="utf-8")
    link = project / "link.log"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    with pytest.raises(log_inspect.LogInspectError, match="symlink|junction"):
        log_inspect.inspect_log(link)

    candidate = project / "race.log"
    candidate.write_text("INFO safe", encoding="utf-8")
    original = log_inspect.resolve_log_path
    replaced = False

    def race(path, **kwargs):
        nonlocal replaced
        target = original(path, **kwargs)
        if not replaced:
            replaced = True
            candidate.unlink()
            candidate.symlink_to(outside)
        return target

    monkeypatch.setattr(log_inspect, "resolve_log_path", race)
    with pytest.raises(log_inspect.LogInspectError, match="symlink|junction|safely"):
        log_inspect.inspect_log(candidate)
    assert replaced


def test_no_caller_regex_or_execution_network_surface():
    parameters = inspect.signature(log_inspect.inspect_log).parameters
    assert "regex" not in parameters and "pattern" not in parameters
    source = Path(log_inspect.__file__).read_text(encoding="utf-8")
    for forbidden in ("subprocess", "urllib", "requests", "socket", "shell=True", "eval(", "exec("):
        assert forbidden not in source


def test_server_read_only_project_activity_dedup_and_autopilot(project):
    path = project / "app.log"
    path.write_text("INFO start\nERROR boom\n", encoding="utf-8")
    activity_tracker.reset_for_tests()

    assert server.mcp._tool_manager.get_tool("log_inspect") is not None
    assert "log_inspect" in server.tool_manifest()
    assert "log_inspect" in server._agent_tool_help(read_only=True)
    assert "log_inspect" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "log_inspect" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "log_inspect" in server._WORK_INSPECTION_TOOLS
    assert "log_inspect" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "log_inspect" in server._AUTOPILOT_OBSERVE_TOOLS

    with activity_tracker.response_span("test", "inspect log"):
        output = server._agent_dispatch_observed(
            "log_inspect", {"path": "app.log"},
            read_only=True, project=str(project),
        )
    report = json.loads(output)
    assert report["summary"]["error_lines"] == 1
    event = next(
        row for row in activity_tracker.latest()["events"]
        if row.get("kind") == "log_inspect"
    )
    assert event["path"] == str(path.resolve())


def test_project_scope_escape_and_signature_normalization(project):
    scoped = server._project_scope_args(
        "log_inspect", {"path": "logs/../app.log"}, str(project),
    )
    escaped = server._project_scope_args(
        "log_inspect", {"path": "../outside.log"}, str(project),
    )
    assert Path(scoped["path"]).resolve() == (project / "app.log").resolve()
    assert "outside" in server._repository_scope_path_error(
        "log_inspect", escaped, str(project),
    )
    first = server._agent_call_signature(
        "log_inspect", {"path": str(project / "logs" / ".." / "app.log")},
    )
    second = server._agent_call_signature(
        "log_inspect", {"path": str(project / "app.log")},
    )
    assert first == second


def test_read_only_policy_forbids_model_extra_roots(project):
    error = server._repository_read_only_error(
        "log_inspect", {"path": "app.log", "extra_roots": str(project)},
    )
    assert "forbids argument(s): extra_roots" in error
