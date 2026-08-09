"""Safety, bounds, determinism, and integration for workspace_compare."""
import json
import os
from pathlib import Path
import time

import pytest

import file_ops
import server
import workspace_compare


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    return root


def _trees(workspace):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_text("same", encoding="utf-8")
    (right / "same.txt").write_text("same", encoding="utf-8")
    (left / "changed.txt").write_text("before", encoding="utf-8")
    (right / "changed.txt").write_text("after", encoding="utf-8")
    (left / "removed.txt").write_text("removed", encoding="utf-8")
    (right / "added.txt").write_text("added", encoding="utf-8")
    (left / "empty-left").mkdir()
    (right / "empty-right").mkdir()
    return left, right


def test_directory_comparison_is_deterministic_metadata_only(workspace):
    left, right = _trees(workspace)
    first = workspace_compare.compare_workspaces(left, right)
    second = workspace_compare.compare_workspaces(left, right)
    assert workspace_compare.encode_result(first) == workspace_compare.encode_result(second)
    assert first["summary"] == {"added": 2, "removed": 2, "changed": 1, "same": 2}
    assert [row["path"] for row in first["added"]] == ["added.txt", "empty-right"]
    assert [row["path"] for row in first["removed"]] == ["empty-left", "removed.txt"]
    assert first["changed"][0]["path"] == "changed.txt"
    assert first["same"][0]["path"] == "."
    encoded = workspace_compare.encode_result(first)
    assert "before" not in encoded and "after" not in encoded
    assert all(
        len(row["sha256"]) == 64
        for row in first["added"] + first["removed"]
        if row["type"] == "file"
    )


def test_file_roots_compare_at_dot_and_never_expose_contents(workspace):
    left = workspace / "left.txt"
    right = workspace / "right.txt"
    left.write_text("private left phrase", encoding="utf-8")
    right.write_text("private right phrase", encoding="utf-8")
    report = workspace_compare.compare_workspaces(left, right)
    assert report["summary"] == {"added": 0, "removed": 0, "changed": 1, "same": 0}
    assert report["changed"][0]["path"] == "."
    output = workspace_compare.encode_result(report)
    assert "private left phrase" not in output
    assert "private right phrase" not in output


def test_type_change_is_changed_and_descendants_are_added(workspace):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    (left / "node").write_text("file", encoding="utf-8")
    (right / "node").mkdir()
    (right / "node" / "child.txt").write_text("child", encoding="utf-8")
    report = workspace_compare.compare_workspaces(left, right)
    assert report["summary"] == {"added": 1, "removed": 0, "changed": 1, "same": 1}
    assert report["changed"][0]["left"]["type"] == "file"
    assert report["changed"][0]["right"]["type"] == "directory"


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({"max_entries": 2}, "entry ceiling"),
        ({"max_file_bytes": 3}, "per-file byte ceiling"),
        ({"max_total_bytes": 7}, "total byte ceiling"),
    ],
)
def test_scan_caps_fail_closed(workspace, kwargs, error):
    if "max_entries" in kwargs:
        left = workspace / "left"
        right = workspace / "right"
        left.mkdir()
        right.mkdir()
        (left / "child.txt").write_text("1234", encoding="utf-8")
    else:
        left = workspace / "left.txt"
        right = workspace / "right.txt"
        left.write_text("1234", encoding="utf-8")
        right.write_text("1234", encoding="utf-8")
    with pytest.raises(workspace_compare.WorkspaceCompareError, match=error):
        workspace_compare.compare_workspaces(left, right, **kwargs)


def test_output_and_detail_caps_preserve_exact_summary(workspace):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    for number in range(80):
        (right / ("file-%03d.txt" % number)).write_text(str(number), encoding="utf-8")
    report = workspace_compare.compare_workspaces(
        left, right, max_details=10_000, max_output_bytes=1024,
    )
    output = workspace_compare.encode_result(report).encode("utf-8")
    assert report["summary"]["added"] == 80
    assert report["details_truncated"] is True
    assert len(report["added"]) < 80
    assert len(output) <= 1024
    assert report["output_bytes"] == len(output)


def test_output_fitting_uses_logarithmic_serialization_and_deadline(monkeypatch):
    rows = [
        {
            "path": "file-%05d.txt" % number, "type": "file", "size": 1,
            "sha256": "a" * 64,
        }
        for number in range(10_000)
    ]
    report = {
        "ok": True, "left": {}, "right": {},
        "summary": {"added": len(rows), "removed": 0, "changed": 0, "same": 0},
        "added": rows, "removed": [], "changed": [], "same": [],
        "details_truncated": False, "scan": {}, "limits": {}, "output_bytes": 0,
    }
    original = workspace_compare._encoded
    calls = []

    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(workspace_compare, "_encoded", counted)
    fitted = workspace_compare._fit_output(
        report, 1024, time.monotonic() + 5,
    )
    assert len(workspace_compare.encode_result(fitted).encode("utf-8")) <= 1024
    assert fitted["details_truncated"] is True
    assert len(calls) < 100
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="timeout ceiling"):
        workspace_compare._fit_output(report, 1024, time.monotonic() - 1)

    def slow_encode(value):
        time.sleep(0.06)
        return original(value)

    monkeypatch.setattr(workspace_compare, "_encoded", slow_encode)
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="timeout ceiling"):
        workspace_compare._fit_output(report, 1024, time.monotonic() + 0.05)


@pytest.mark.parametrize("name", [".env", "credentials.json", "private.pem"])
def test_sensitive_descendant_rejects_entire_comparison(workspace, name):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    (left / name).write_text("secret", encoding="utf-8")
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="secret|control"):
        workspace_compare.compare_workspaces(left, right)


def test_control_directory_rejects_entire_comparison(workspace):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    metadata = left / ".git"
    metadata.mkdir()
    (metadata / "config").write_text("secret", encoding="utf-8")
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="secret|control"):
        workspace_compare.compare_workspaces(left, right)


@pytest.mark.parametrize("root_name", [".git", ".ssh"])
def test_sensitive_allowed_root_itself_is_rejected(tmp_path, monkeypatch, root_name):
    sensitive_root = tmp_path / root_name
    home = tmp_path / "home"
    sensitive_root.mkdir()
    home.mkdir()
    left = sensitive_root / "left.txt"
    right = sensitive_root / "right.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: sensitive_root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="secret|control"):
        workspace_compare.compare_workspaces(left, right)


def test_directory_child_added_during_hashing_rejects_exact_report(workspace, monkeypatch):
    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("same", encoding="utf-8")
    (right / "a.txt").write_text("same", encoding="utf-8")
    original = workspace_compare._hash_file
    mutated = False

    def mutate_after_hash(path, budget):
        nonlocal mutated
        result = original(path, budget)
        if path.parent == left and not mutated:
            (left / "late.txt").write_text("late", encoding="utf-8")
            mutated = True
        return result

    monkeypatch.setattr(workspace_compare, "_hash_file", mutate_after_hash)
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="directory.*changed"):
        workspace_compare.compare_workspaces(left, right)
    assert mutated


def test_symlink_root_and_descendant_are_rejected(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    regular = workspace / "regular.txt"
    regular.write_text("regular", encoding="utf-8")
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="symlink|junction"):
        workspace_compare.compare_workspaces(link, regular)

    left = workspace / "left"
    right = workspace / "right"
    left.mkdir()
    right.mkdir()
    (left / "nested-link").symlink_to(outside)
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="symlink|junction"):
        workspace_compare.compare_workspaces(left, right)


def test_opened_handle_identity_check_rejects_swap_race(workspace, monkeypatch):
    left = workspace / "left.txt"
    right = workspace / "right.txt"
    replacement = workspace / "replacement.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")
    real_open = workspace_compare.os.open
    swapped = {"done": False}

    def racing_open(path, flags):
        if Path(path) == left and not swapped["done"]:
            left.unlink()
            replacement.rename(left)
            swapped["done"] = True
        return real_open(path, flags)

    monkeypatch.setattr(workspace_compare.os, "open", racing_open)
    with pytest.raises(workspace_compare.WorkspaceCompareError, match="identity changed"):
        workspace_compare.compare_workspaces(left, right)


def test_hash_open_uses_nofollow_flag_when_platform_exposes_it(workspace, monkeypatch):
    left = workspace / "left.txt"
    right = workspace / "right.txt"
    left.write_text("left", encoding="utf-8")
    right.write_text("right", encoding="utf-8")
    real_open = workspace_compare.os.open
    flags_seen = []

    def recording_open(path, flags):
        flags_seen.append(flags)
        return real_open(path, flags)

    monkeypatch.setattr(workspace_compare.os, "open", recording_open)
    workspace_compare.compare_workspaces(left, right)
    if getattr(os, "O_NOFOLLOW", 0):
        assert all(flags & os.O_NOFOLLOW for flags in flags_seen)


def test_project_scope_manifest_dispatch_activity_and_read_only(
    tmp_path, monkeypatch,
):
    sonder_root = tmp_path / "sonder"
    project = tmp_path / "project"
    home = tmp_path / "home"
    sonder_root.mkdir()
    project.mkdir()
    home.mkdir()
    (project / "left").mkdir()
    (project / "right").mkdir()
    (project / "left" / "a.txt").write_text("one", encoding="utf-8")
    (project / "right" / "a.txt").write_text("two", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: sonder_root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    calls = []
    monkeypatch.setattr(
        server.activity_tracker, "record_tool_result",
        lambda name, args, **kwargs: calls.append((name, kwargs)),
    )
    output = server._agent_dispatch(
        "workspace_compare", {"left": "left", "right": "right"},
        read_only=True, repository_extra_roots=str(project),
    )
    report = json.loads(output)
    assert report["summary"]["changed"] == 1
    assert calls[-1][0] == "workspace_compare" and calls[-1][1]["ok"] is True
    assert "workspace_compare" in server.tool_manifest()
    assert "- workspace_compare:" in server._agent_tool_help(read_only=True)
    assert "workspace_compare" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "workspace_compare" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "workspace_compare" in server._WORK_INSPECTION_TOOLS
    assert "workspace_compare" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "workspace_compare" in server._AUTOPILOT_OBSERVE_TOOLS


def test_project_scope_rejects_either_side_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    scoped = server._project_scope_args(
        "workspace_compare", {"left": "left", "right": str(outside)},
        str(project),
    )
    assert "outside" in server._repository_scope_path_error(
        "workspace_compare", scoped, str(project),
    )


def test_live_reload_rebinds_helper_alias_without_replacing_tool(monkeypatch):
    original_tool = server.workspace_compare
    original_module = server.workspace_compare_module
    replacement_module = object()

    class ReloadStub:
        @staticmethod
        def reload_changed_modules(names):
            assert "workspace_compare" in names
            return {"workspace_compare": replacement_module}

    monkeypatch.setattr(server, "live_reload", ReloadStub)
    monkeypatch.setattr(server, "workspace_compare_module", original_module)
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda create=True: None)
    server._maybe_live_reload()

    assert server.workspace_compare is original_tool
    assert server.workspace_compare_module is replacement_module
