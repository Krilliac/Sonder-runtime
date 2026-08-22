import hashlib
import json
import os
from pathlib import Path

import pytest

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
from sonder_runtime.adapters.inspection import content_digest
import sonder_runtime.adapters.filesystem.file_ops as file_ops
import server


@pytest.fixture
def project(monkeypatch, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    return root


def _directory(project, **kwargs):
    return content_digest.digest_directory(str(project), **kwargs)


def test_file_digest_streams_binary_sha256(project, monkeypatch):
    payload = bytes(range(256)) * 400
    target = project / "artifact.bin"
    target.write_bytes(payload)

    def forbidden(*args, **kwargs):
        raise AssertionError("whole-file convenience reads are forbidden")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    data = content_digest.digest_file(str(target))

    assert data["algorithm"] == "sha256"
    assert data["sha256"] == hashlib.sha256(payload).hexdigest()
    assert data["bytes"] == len(payload)
    assert not data["truncated"]


def test_unicode_relative_paths_and_json_are_deterministic(project):
    nested = project / "données"
    nested.mkdir()
    (nested / "雪.txt").write_text("café\n", encoding="utf-8")

    first = _directory(project)
    second = _directory(project)
    rendered = content_digest.format_digest(first)

    assert first == second
    assert first["manifest"][0]["path"] == "données/雪.txt"
    assert "données/雪.txt" in rendered
    assert json.loads(rendered) == first


def test_file_and_directory_digests_change_after_mutation(project):
    target = project / "state.dat"
    target.write_bytes(b"before")
    file_before = content_digest.digest_file(str(target))["sha256"]
    tree_before = _directory(project)["merkle_sha256"]

    target.write_bytes(b"after")
    file_after = content_digest.digest_file(str(target))["sha256"]
    tree_after = _directory(project)["merkle_sha256"]

    assert file_before != file_after
    assert tree_before != tree_after


def test_manifest_order_and_merkle_ignore_creation_order_and_root(project, tmp_path, monkeypatch):
    (project / "z.txt").write_text("z", encoding="utf-8")
    (project / "a.txt").write_text("a", encoding="utf-8")
    (project / "nested").mkdir()
    (project / "nested" / "m.txt").write_text("m", encoding="utf-8")
    first = _directory(project)

    other = tmp_path / "other"
    (other / "nested").mkdir(parents=True)
    (other / "nested" / "m.txt").write_text("m", encoding="utf-8")
    (other / "a.txt").write_text("a", encoding="utf-8")
    (other / "z.txt").write_text("z", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: other)
    second = content_digest.digest_directory(str(other))

    expected = ["a.txt", "nested/m.txt", "z.txt"]
    assert [row["path"] for row in first["manifest"]] == expected
    assert [row["path"] for row in second["manifest"]] == expected
    assert first["merkle_sha256"] == second["merkle_sha256"]


def test_empty_directory_has_a_complete_fixed_merkle(project):
    data = _directory(project)

    assert data["complete"]
    assert data["manifest"] == []
    assert data["merkle_sha256"] == data["partial_merkle_sha256"]
    assert len(data["merkle_sha256"]) == 64


def test_file_byte_cap_never_labels_partial_content_as_digest(project):
    target = project / "large.bin"
    target.write_bytes(b"x" * 100)

    data = content_digest.digest_file(str(target), max_bytes=10)

    assert data["sha256"] is None
    assert data["bytes"] == 0
    assert data["truncated"]
    assert "exceeds byte ceiling" in data["error"]


def test_directory_caps_are_explicit_and_full_merkle_fails_closed(project):
    for name in ("a.bin", "b.bin", "c.bin"):
        (project / name).write_bytes(b"x" * 20)

    files = _directory(project, max_files=1)
    total = _directory(project, max_total_bytes=25)
    per_file = _directory(project, max_file_bytes=10)
    results = _directory(project, max_results=1)

    for data in (files, total, per_file, results):
        assert not data["complete"]
        assert data["merkle_sha256"] is None
        assert len(data["partial_merkle_sha256"]) == 64
    assert files["truncation_reasons"] == ["max_files"]
    assert total["truncation_reasons"] == ["max_total_bytes"]
    assert per_file["truncation_reasons"] == ["max_file_bytes"]
    assert results["truncation_reasons"] == ["max_results"]
    assert len(per_file["errors"]) == 3


def test_depth_and_hard_ceilings_are_bounded(project):
    nested = project / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("deep", encoding="utf-8")

    shallow = _directory(project, max_depth=1)
    clamped = _directory(
        project, max_depth=10**9, max_files=10**9,
        max_total_bytes=10**9, max_file_bytes=10**9, max_results=10**9,
    )

    assert shallow["truncation_reasons"] == ["max_depth"]
    assert shallow["merkle_sha256"] is None
    assert clamped["limits"] == {
        "max_depth": content_digest.HARD_MAX_DEPTH,
        "max_files": content_digest.HARD_MAX_FILES,
        "max_total_bytes": content_digest.HARD_MAX_TOTAL_BYTES,
        "max_file_bytes": content_digest.HARD_MAX_FILE_BYTES,
        "max_results": content_digest.HARD_MAX_RESULTS,
    }


def test_sensitive_entries_and_symlinks_are_explicitly_incomplete(project, tmp_path):
    (project / "ok.txt").write_text("ok", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("secret", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = project / "linked.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    data = _directory(project)
    errors = {row["path"]: row["error"] for row in data["errors"]}

    assert [row["path"] for row in data["manifest"]] == ["ok.txt"]
    assert errors[".git"] == "sensitive directory skipped"
    assert errors["linked.txt"] == "symlink or junction skipped"
    assert not data["complete"]
    assert data["merkle_sha256"] is None
    assert not data["truncated"]


def test_symlink_file_and_directory_roots_are_rejected(project, tmp_path):
    target = project / "real.txt"
    target.write_text("real", encoding="utf-8")
    file_link = project / "file-link.txt"
    dir_link = tmp_path / "dir-link"
    try:
        file_link.symlink_to(target)
        dir_link.symlink_to(project, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")

    with pytest.raises(PermissionError, match="symlink or junction"):
        content_digest.digest_file(str(file_link))
    with pytest.raises(PermissionError, match="symlink or junction"):
        content_digest.digest_directory(str(dir_link))


def test_file_replaced_by_symlink_immediately_before_open_is_not_hashed(
    project, tmp_path, monkeypatch,
):
    target = project / "race.bin"
    target.write_bytes(b"safe")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"sensitive outside bytes")
    original = content_digest._stream_sha256

    def race(path, max_bytes, extra_roots=""):
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            pytest.skip("symlink creation unavailable: %s" % exc)
        return original(path, max_bytes, extra_roots)

    monkeypatch.setattr(content_digest, "_stream_sha256", race)

    with pytest.raises((OSError, PermissionError)):
        content_digest.digest_file(str(target))


def test_directory_membership_change_during_hashing_fails_complete_merkle(
    project, monkeypatch,
):
    (project / "first.bin").write_bytes(b"first")
    original = content_digest._stream_sha256
    changed = False

    def mutate(path, max_bytes, extra_roots=""):
        nonlocal changed
        if not changed:
            changed = True
            (project / "late.bin").write_bytes(b"late")
        return original(path, max_bytes, extra_roots)

    monkeypatch.setattr(content_digest, "_stream_sha256", mutate)

    data = _directory(project)

    assert changed
    assert data["complete"] is False
    assert data["merkle_sha256"] is None
    assert any("membership changed" in row["error"] for row in data["errors"])


def test_manifest_digest_escapes_surrogate_filenames_deterministically():
    rows = [{"path": "bad-\udcff.bin", "bytes": 1, "sha256": "0" * 64}]

    first = content_digest._manifest_digest(rows)
    second = content_digest._manifest_digest(rows)
    rendered = content_digest.format_digest({"manifest": rows})

    assert first == second
    assert len(first) == 64
    assert "\\udcff" in rendered


def test_containment_sensitive_and_foreign_absolute_paths(project, tmp_path):
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (project / ".git").mkdir()
    secret = project / ".git" / "index"
    secret.write_bytes(b"secret")

    with pytest.raises(PermissionError, match="outside every authorized root"):
        content_digest.digest_file(str(outside))
    with pytest.raises(PermissionError, match="secret or control state"):
        content_digest.digest_file(str(secret))
    foreign = "C:\\outside\\repo" if os.name != "nt" else "/outside/repo"
    with pytest.raises(PermissionError, match="non-native absolute"):
        content_digest.digest_directory(foreign)


def test_server_discovery_policy_activity_dedup_and_autopilot(project):
    target = project / "artifact.bin"
    target.write_bytes(b"artifact")
    activity_tracker.reset_for_tests()

    for tool in ("file_digest", "directory_digest"):
        assert server.mcp._tool_manager.get_tool(tool) is not None
        assert tool in server.tool_manifest()
        assert tool in server._agent_tool_help(read_only=True)
        assert tool in server.REPOSITORY_READ_ONLY_TOOLS
        assert tool in server._PROJECT_BOUND_AGENT_TOOLS
        assert tool in server._WORK_INSPECTION_TOOLS
        assert tool in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
        assert tool in server._AUTOPILOT_OBSERVE_TOOLS
        assert tool in server._AGENT_FILE_EVIDENCE_TOOLS

    with activity_tracker.response_span("test", "digest artifact"):
        file_output = server._agent_dispatch_observed(
            "file_digest", {"path": "artifact.bin"},
            read_only=True, project=str(project),
        )
        directory_output = server._agent_dispatch_observed(
            "directory_digest", {"path": "."},
            read_only=True, project=str(project),
        )
    assert json.loads(file_output)["sha256"] == hashlib.sha256(b"artifact").hexdigest()
    assert json.loads(directory_output)["complete"]
    kinds = {row.get("kind") for row in activity_tracker.latest()["events"]}
    assert {"file_digest", "directory_digest"}.issubset(kinds)

    first = server._agent_call_signature("directory_digest", {"path": str(project / "src" / "..")})
    second = server._agent_call_signature("directory_digest", {"path": str(project)})
    assert first == second


@pytest.mark.parametrize("tool", ["file_digest", "directory_digest"])
def test_project_scope_escape_and_model_extra_roots_are_rejected(project, tool):
    escaped = server._project_scope_args(tool, {"path": "../outside"}, str(project))
    assert "outside" in server._repository_scope_path_error(tool, escaped, str(project))
    error = server._repository_read_only_error(
        tool, {"path": str(project), "extra_roots": str(project)},
    )
    assert "forbids argument(s): extra_roots" in error


def test_digest_module_has_no_shell_or_network_api():
    source = Path(content_digest.__file__).read_text(encoding="utf-8")
    forbidden = ("subprocess", "urllib", "requests", "http.client", "os.system", "eval(", "exec(")
    assert not [name for name in forbidden if name in source]
