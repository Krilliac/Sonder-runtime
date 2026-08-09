import json
import os
import pytest

import file_ops
import server


def _operation(path, content, mode):
    return {"path": str(path), "content": content, "mode": mode}


def test_batch_create_and_overwrite_commit_in_input_order(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    existing = tmp_path / "existing.txt"
    existing.write_text("before", encoding="utf-8")

    report = file_ops.batch_write_files([
        _operation("nested/new.txt", "new\n", "create"),
        _operation("existing.txt", "after", "overwrite"),
    ])

    assert report["ok"] is True
    assert report["transaction"] == "committed"
    assert [row["index"] for row in report["results"]] == [0, 1]
    assert [row["status"] for row in report["results"]] == ["written", "written"]
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "new\n"
    assert existing.read_text(encoding="utf-8") == "after"


@pytest.mark.parametrize(
    "bad_operation,error",
    [
        ({"path": "bad.txt", "content": "x"}, "explicitly"),
        (_operation("bad.txt", "x", "append"), "explicitly"),
        ({"path": "bad.txt", "content": "x", "mode": "create", "extra": 1}, "unsupported"),
        ({"path": "bad.txt", "content": 1, "mode": "create"}, "content must"),
    ],
)
def test_prevalidation_rejects_entire_batch_before_any_write(
    monkeypatch, tmp_path, bad_operation, error,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("would-have-been-created.txt", "ok", "create"),
            bad_operation,
        ])
    assert caught.value.report["transaction"] == "not_started"
    assert error in caught.value.report["results"][1]["error"]
    assert not (tmp_path / "would-have-been-created.txt").exists()


def test_batch_bounds_count_per_file_and_aggregate(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops, "MAX_BATCH_JSON_BYTES", 10)
    with pytest.raises(ValueError, match="max batch input bytes"):
        file_ops.batch_write_files(
            json.dumps([_operation("one.txt", "x", "create")])
        )
    monkeypatch.setattr(file_ops, "MAX_BATCH_JSON_BYTES", 4_128_000)
    monkeypatch.setattr(file_ops, "MAX_BATCH_FILES", 2)
    with pytest.raises(ValueError, match="max file count"):
        file_ops.batch_write_files([
            _operation("%d.txt" % index, "x", "create") for index in range(3)
        ])

    monkeypatch.setattr(file_ops, "MAX_WRITE_BYTES", 3)
    with pytest.raises(file_ops.BatchWriteError) as per_file:
        file_ops.batch_write_files([_operation("large.txt", "four", "create")])
    assert "max write bytes" in per_file.value.report["results"][0]["error"]

    monkeypatch.setattr(file_ops, "MAX_WRITE_BYTES", 10)
    monkeypatch.setattr(file_ops, "MAX_BATCH_BYTES", 5)
    with pytest.raises(file_ops.BatchWriteError) as aggregate:
        file_ops.batch_write_files([
            _operation("one.txt", "abc", "create"),
            _operation("two.txt", "def", "create"),
        ])
    assert "aggregate content" in aggregate.value.report["results"][1]["error"]
    assert not (tmp_path / "one.txt").exists()

    snapshot = tmp_path / "snapshot.txt"
    snapshot.write_text("original", encoding="utf-8")
    monkeypatch.setattr(file_ops, "MAX_BATCH_SNAPSHOT_BYTES", 3)
    with pytest.raises(file_ops.BatchWriteError) as snapshots:
        file_ops.batch_write_files([
            _operation("snapshot.txt", "new", "overwrite"),
        ])
    assert "aggregate rollback snapshot" in snapshots.value.report["results"][0]["error"]
    assert snapshot.read_text(encoding="utf-8") == "original"


def test_duplicate_resolved_target_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("same.txt", "one", "create"),
            _operation("folder/../same.txt", "two", "create"),
        ])
    assert "duplicate batch target" in caught.value.report["results"][1]["error"]
    assert not (tmp_path / "same.txt").exists()


def test_sensitive_and_outside_targets_are_rejected(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    with pytest.raises(file_ops.BatchWriteError) as sensitive:
        file_ops.batch_write_files([_operation(".env", "SECRET=x", "create")])
    assert "secret or control state" in sensitive.value.report["results"][0]["error"]

    with pytest.raises(file_ops.BatchWriteError) as metadata:
        file_ops.batch_write_files([
            _operation(".git/config", "[core]", "create"),
        ])
    assert "secret or control state" in metadata.value.report["results"][0]["error"]

    outside = tmp_path / "outside.txt"
    with pytest.raises(file_ops.BatchWriteError) as escaped:
        file_ops.batch_write_files([_operation(outside, "no", "create")])
    assert "outside allowed roots" in escaped.value.report["results"][0]["error"]
    assert not outside.exists()

    foreign = "/etc/sonder-escape" if os.name == "nt" else "C:\\sonder-escape.txt"
    with pytest.raises(file_ops.BatchWriteError) as non_native:
        file_ops.batch_write_files([_operation(foreign, "no", "create")])
    assert "non-native absolute" in non_native.value.report["results"][0]["error"]


def test_symlink_escape_is_rejected(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)

    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("linked/escape.txt", "no", "create"),
        ])
    assert "symlink or junction" in caught.value.report["results"][0]["error"]
    assert not (outside / "escape.txt").exists()


def test_partial_failure_restores_overwrites_and_removes_creates(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    existing = tmp_path / "existing.bin"
    original = b"\xff\x00original\r\n"
    existing.write_bytes(original)
    untouched = tmp_path / "untouched.txt"
    untouched.write_text("unchanged", encoding="utf-8")
    real_write = file_ops.write_file
    calls = []

    def fail_before_third_write(*args, **kwargs):
        if len(calls) == 2:
            raise OSError("injected pre-write failure")
        result = real_write(*args, **kwargs)
        calls.append(str(args[0]))
        return result

    monkeypatch.setattr(file_ops, "write_file", fail_before_third_write)
    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("new/created.txt", "created", "create"),
            _operation("existing.bin", "replacement", "overwrite"),
            _operation("untouched.txt", "changed", "overwrite"),
        ])

    report = caught.value.report
    assert report["transaction"] == "rolled_back"
    assert report["failed_index"] == 2
    assert [row["status"] for row in report["results"]] == [
        "rolled_back", "rolled_back", "failed",
    ]
    assert [row["index"] for row in report["rollback"]] == [0, 1]
    assert all(row["restored"] for row in report["rollback"])
    assert not (tmp_path / "new" / "created.txt").exists()
    assert not (tmp_path / "new").exists()
    assert existing.read_bytes() == original
    assert untouched.read_text(encoding="utf-8") == "unchanged"


def test_failed_create_does_not_remove_concurrently_created_file(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    real_write = file_ops.write_file

    def concurrent_create(path, content, **kwargs):
        target = tmp_path / str(path)
        target.write_text("other process", encoding="utf-8")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(file_ops, "write_file", concurrent_create)
    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("raced.txt", "batch", "create"),
        ])

    assert caught.value.report["rollback"] == []
    assert (tmp_path / "raced.txt").read_text(encoding="utf-8") == "other process"


def test_hard_link_aliases_are_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("original", encoding="utf-8")
    try:
        second.hardlink_to(first)
    except OSError as exc:
        pytest.skip("hard links unavailable: %s" % exc)

    with pytest.raises(file_ops.BatchWriteError) as caught:
        file_ops.batch_write_files([
            _operation("first.txt", "one", "overwrite"),
            _operation("second.txt", "two", "overwrite"),
        ])

    assert "file identity" in caught.value.report["results"][1]["error"]
    assert first.read_text(encoding="utf-8") == "original"
    assert second.read_text(encoding="utf-8") == "original"


def test_mcp_manifest_help_dispatch_and_activity(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    changes = []
    monkeypatch.setattr(
        server.activity_tracker, "record_file_change",
        lambda action, path, **kwargs: changes.append((action, path)),
    )
    output = server._agent_dispatch("file_batch_write", {
        "operations": [_operation("agent.txt", "hello", "create")],
    })
    report = json.loads(output)
    assert report["transaction"] == "committed"
    assert (tmp_path / "agent.txt").read_text(encoding="utf-8") == "hello"
    assert any(path.endswith("agent.txt") for _, path in changes)
    assert "file_batch_write" in server.tool_manifest()
    assert "- file_batch_write:" in server._agent_tool_help()
    assert "file_batch_write" in server._WORK_MUTATION_TOOLS
    assert "file_batch_write" in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "file_batch_write" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert server._agent_dispatch(
        "file_batch_write", {"operations": []}, read_only=True,
    ).startswith("ERROR:")


def test_project_scope_rebases_every_batch_target_and_rejects_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    args = {
        "operations": [
            _operation("src/a.py", "a", "create"),
            _operation("tests/a.py", "b", "create"),
        ],
    }
    scoped = server._project_scope_args("file_batch_write", args, str(project))
    operations = json.loads(scoped["operations_json"])
    assert operations[0]["path"] == str(project / "src" / "a.py")
    assert operations[1]["path"] == str(project / "tests" / "a.py")
    assert server._repository_scope_path_error(
        "file_batch_write", scoped, str(project),
    ) == ""

    operations[1]["path"] = str(tmp_path / "escape.py")
    scoped["operations_json"] = json.dumps(operations)
    assert "outside" in server._repository_scope_path_error(
        "file_batch_write", scoped, str(project),
    )


def test_project_bound_dispatch_uses_same_host_selected_scope(
    monkeypatch, tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))
    output = server._agent_dispatch_observed(
        "file_batch_write",
        {"operations": [_operation("inside.txt", "ok", "create")]},
        project=str(project),
    )
    assert json.loads(output)["transaction"] == "committed"
    assert (project / "inside.txt").read_text(encoding="utf-8") == "ok"
