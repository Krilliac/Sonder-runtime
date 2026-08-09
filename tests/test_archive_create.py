from __future__ import annotations

import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

import activity_tracker
import archive_create
import file_ops
import server


@pytest.fixture()
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _create(project, inputs, destination, **kwargs):
    return archive_create.create_archive(
        str(project), inputs, destination, **kwargs,
    )


@pytest.mark.parametrize("archive_format", ["zip", "tar"])
def test_deterministic_archives_preserve_binary_unicode_and_empty_dirs(
    project, archive_format,
):
    source = project / "payload"
    (source / "unicode").mkdir(parents=True)
    (source / "empty").mkdir()
    payload = bytes(range(256)) + b"\x00\xff"
    (source / "binary.bin").write_bytes(payload)
    (source / "unicode" / "\u96ea.txt").write_text("caf\u00e9", encoding="utf-8")
    suffix = ".zip" if archive_format == "zip" else ".tar"

    first = _create(
        project, ["payload"], "first" + suffix,
        archive_format=archive_format, deterministic=True,
    )
    second = _create(
        project, ["payload"], "second" + suffix,
        archive_format=archive_format, deterministic=True,
    )

    assert (project / ("first" + suffix)).read_bytes() == (project / ("second" + suffix)).read_bytes()
    assert first["archive_sha256"] == second["archive_sha256"]
    assert first["files"] == 2
    assert first["overwrote"] is False
    expected = {"payload", "payload/binary.bin", "payload/empty", "payload/unicode", "payload/unicode/\u96ea.txt"}
    assert {row["path"] for row in first["entries"]} == expected
    if archive_format == "zip":
        with zipfile.ZipFile(project / ("first" + suffix)) as archive:
            assert archive.read("payload/binary.bin") == payload
            assert archive.read("payload/unicode/\u96ea.txt").decode("utf-8") == "caf\u00e9"
            assert archive.getinfo("payload/binary.bin").date_time == (1980, 1, 1, 0, 0, 0)
    else:
        with tarfile.open(project / ("first" + suffix)) as archive:
            assert archive.extractfile("payload/binary.bin").read() == payload
            assert archive.extractfile("payload/unicode/\u96ea.txt").read().decode("utf-8") == "caf\u00e9"
            assert archive.getmember("payload/binary.bin").mtime == 0


def test_destination_is_non_overwriting_and_cannot_be_inside_input(project):
    source = project / "payload"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    existing = project / "existing.zip"
    existing.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        _create(project, ["payload/a.txt"], "existing.zip")
    with pytest.raises(archive_create.ArchiveCreateRejected, match="inside an input"):
        _create(project, ["payload"], "payload/archive.zip")

    assert existing.read_bytes() == b"keep"
    assert not (source / "archive.zip").exists()


def test_nondeterministic_option_preserves_source_mtime(project):
    source = project / "input.txt"
    source.write_text("input", encoding="utf-8")
    timestamp = 1_600_000_000
    os.utime(source, (timestamp, timestamp))

    result = _create(
        project, ["input.txt"], "output.tar",
        archive_format="tar", deterministic=False,
    )

    assert result["deterministic"] is False
    with tarfile.open(project / "output.tar") as archive:
        assert archive.getmember("input.txt").mtime == timestamp


def test_caps_and_result_truncation_are_explicit(project):
    source = project / "payload"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a" * 10)
    (source / "b.bin").write_bytes(b"b" * 10)

    with pytest.raises(archive_create.ArchiveCreateRejected, match="max_files"):
        _create(project, ["payload"], "files.zip", max_files=1)
    with pytest.raises(archive_create.ArchiveCreateRejected, match="max_file_bytes"):
        _create(project, ["payload"], "file.zip", max_file_bytes=5)
    with pytest.raises(archive_create.ArchiveCreateRejected, match="max_total_bytes"):
        _create(project, ["payload"], "total.zip", max_total_bytes=15)
    with pytest.raises(archive_create.ArchiveCreateRejected, match="max_entries"):
        _create(project, ["payload"], "entries.zip", max_entries=2)
    result = _create(project, ["payload"], "results.zip", max_results=1)
    assert result["truncated"] is True
    assert result["omitted_results"] == 2
    assert len(result["entries"]) == 1


def test_depth_duplicate_and_format_validation(project):
    nested = project / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "a.txt").write_text("a", encoding="utf-8")

    with pytest.raises(archive_create.ArchiveCreateRejected, match="max_depth"):
        _create(project, ["one"], "deep.zip", max_depth=1)
    with pytest.raises(archive_create.ArchiveCreateRejected, match="duplicate"):
        _create(project, ["one", "one/two/a.txt"], "duplicate.zip")
    with pytest.raises(ValueError, match="zip or tar"):
        _create(project, ["one"], "bad.7z", archive_format="7z")


def test_sensitive_control_and_outside_inputs_are_rejected(project, tmp_path):
    (project / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    for source in (".env", ".git", str(outside)):
        with pytest.raises(PermissionError):
            _create(project, [source], "denied.zip")
    assert not (project / "denied.zip").exists()


def test_symlink_file_and_tree_links_are_rejected(project, tmp_path):
    payload = project / "payload"
    payload.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = payload / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    with pytest.raises(PermissionError):
        _create(project, ["payload"], "linked.zip")
    assert not (project / "linked.zip").exists()


def test_authorized_internal_symlink_alias_is_still_rejected(project):
    target = project / "target.txt"
    target.write_text("target", encoding="utf-8")
    link = project / "alias.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    with pytest.raises(PermissionError, match="symlink or junction"):
        _create(project, ["alias.txt"], "linked.zip")


def test_file_mutation_after_write_rolls_back(project, monkeypatch):
    source = project / "input.bin"
    source.write_bytes(b"before")
    original = archive_create._write_zip

    def mutate(stage, plan, deterministic, extra_roots):
        original(stage, plan, deterministic, extra_roots)
        source.write_bytes(b"after")

    monkeypatch.setattr(archive_create, "_write_zip", mutate)

    with pytest.raises(archive_create.ArchiveCreateRejected, match="changed"):
        _create(project, ["input.bin"], "output.zip")
    assert not (project / "output.zip").exists()
    assert list(project.glob(".sonder-archive-create-*")) == []


def test_directory_membership_mutation_rolls_back(project, monkeypatch):
    source = project / "payload"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    original = archive_create._write_zip

    def mutate(stage, plan, deterministic, extra_roots):
        original(stage, plan, deterministic, extra_roots)
        (source / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(archive_create, "_write_zip", mutate)

    with pytest.raises(archive_create.ArchiveCreateRejected, match="directory"):
        _create(project, ["payload"], "output.zip")
    assert not (project / "output.zip").exists()
    assert list(project.glob(".sonder-archive-create-*")) == []


def test_writer_failure_cleans_staging_file(project, monkeypatch):
    (project / "input.txt").write_text("input", encoding="utf-8")

    def fail(stage, plan, deterministic, extra_roots):
        stage.write(b"partial")
        raise OSError("simulated writer failure")

    monkeypatch.setattr(archive_create, "_write_zip", fail)

    with pytest.raises(OSError, match="simulated"):
        _create(project, ["input.txt"], "output.zip")
    assert not (project / "output.zip").exists()
    assert list(project.glob(".sonder-archive-create-*")) == []


def test_destination_appearing_during_creation_is_preserved(project, monkeypatch):
    (project / "input.txt").write_text("input", encoding="utf-8")
    destination = project / "output.zip"
    original = archive_create._write_zip

    def race(stage, plan, deterministic, extra_roots):
        original(stage, plan, deterministic, extra_roots)
        destination.write_bytes(b"other writer")

    monkeypatch.setattr(archive_create, "_write_zip", race)

    with pytest.raises(FileExistsError, match="appeared"):
        _create(project, ["input.txt"], "output.zip")
    assert destination.read_bytes() == b"other writer"
    assert list(project.glob(".sonder-archive-create-*")) == []


def test_module_has_no_shell_or_network_api():
    source = Path(archive_create.__file__).read_text(encoding="utf-8")
    forbidden = ("subprocess", "urllib", "requests", "http.client", "os.system", "eval(", "exec(")
    assert not [name for name in forbidden if name in source]


def test_server_discovery_project_scope_activity_and_autopilot(project, tmp_path):
    (project / "input.txt").write_text("input", encoding="utf-8")
    activity_tracker.reset_for_tests()

    assert server.mcp._tool_manager.get_tool("archive_create") is not None
    assert "archive_create" in server.tool_manifest()
    assert "- archive_create:" in server._agent_tool_help(read_only=False)
    assert "archive_create" not in server.REPOSITORY_READ_ONLY_TOOLS
    assert "archive_create" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "archive_create" in server._WORK_MUTATION_TOOLS
    assert "archive_create" in server._WORK_VALIDATION_TOOLS
    assert "archive_create" in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "archive_create" not in server._AUTOPILOT_OBSERVE_TOOLS

    with activity_tracker.response_span("test", "create archive"):
        output = server._agent_dispatch_observed(
            "archive_create",
            {
                "root": ".", "inputs_json": ["input.txt"],
                "destination": "output.zip", "archive_format": "zip",
            },
            project=str(project),
        )
    data = json.loads(output)
    assert data["ok"] is True
    assert (project / "output.zip").exists()
    changes = activity_tracker.latest()["files"]
    assert any(row["path"] == str(project / "output.zip") for row in changes)

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    escaped = server._project_scope_args(
        "archive_create",
        {"root": ".", "inputs_json": [str(outside)], "destination": "bad.zip"},
        str(project),
    )
    assert "outside" in server._repository_scope_path_error(
        "archive_create", escaped, str(project),
    )
    escaped_destination = server._project_scope_args(
        "archive_create",
        {"root": ".", "inputs_json": ["input.txt"], "destination": "../bad.zip"},
        str(project),
    )
    assert "outside" in server._repository_scope_path_error(
        "archive_create", escaped_destination, str(project),
    )


def test_archive_create_is_self_validating_for_agent_bookkeeping(project):
    args = {
        "root": str(project), "inputs_json": ["input.txt"],
        "destination": "output.zip",
    }
    destination = server._agent_normalized_path(project / "output.zip")
    mutations = [{"tool": "archive_create", "path": destination}]
    observation = json.dumps({"ok": True, "archive_sha256": "a" * 64})

    assert server._agent_validation_covers(
        "archive_create", args, mutations, observation,
    )
