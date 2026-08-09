import json
import os
import stat
from pathlib import Path

import pytest

import server
import text_patch


def run(root: Path, patch: str, apply=False):
    return text_patch.text_patch(str(root), patch, apply=apply, extra_roots=str(root))


MODIFY = "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+three\n"
CREATE = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,2 @@\n+hello\n+world\n"


def test_preview_and_apply_modify(tmp_path):
    target = tmp_path / "a.txt"; target.write_text("one\ntwo\n", encoding="utf-8")
    preview = run(tmp_path, MODIFY)
    assert preview["ok"] and not preview["applied"]
    assert target.read_text(encoding="utf-8") == "one\ntwo\n"
    result = run(tmp_path, MODIFY, True)
    assert result["transaction"] == "committed"
    assert target.read_text(encoding="utf-8") == "one\nthree\n"
    assert result["files"][0]["additions"] == 1


def test_create_and_no_final_newline(tmp_path):
    result = run(tmp_path, "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hello\n\\ No newline at end of file\n", True)
    assert result["applied"]
    assert (tmp_path / "new.txt").read_bytes() == b"hello"


def test_zero_length_insert_and_delete_ranges(tmp_path):
    target = tmp_path / "a.txt"; target.write_text("one\ntwo\n")
    insertion = "--- a/a.txt\n+++ b/a.txt\n@@ -1,0 +2 @@\n+middle\n"
    run(tmp_path, insertion, True)
    assert target.read_text() == "one\nmiddle\ntwo\n"
    deletion = "--- a/a.txt\n+++ b/a.txt\n@@ -2 +1,0 @@\n-middle\n"
    run(tmp_path, deletion, True)
    assert target.read_text() == "one\ntwo\n"


def test_full_preflight_prevents_partial_write(tmp_path):
    (tmp_path / "a.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("actual\n", encoding="utf-8")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
             "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-wrong\n+new\n")
    with pytest.raises(ValueError, match="exactly match"):
        run(tmp_path, patch, True)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("patch, message", [
    ("--- a/a\n+++ /dev/null\n@@ -1,0 +0,0 @@\n", "deletion"),
    ("--- a/a\n+++ b/b\n@@ -1 +1 @@\n-a\n+b\n", "rename"),
    ("--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n", "parent"),
    ("--- C:/x\n+++ C:/x\n@@ -1 +1 @@\n-a\n+b\n", "relative"),
    ("--- a/a\\b\n+++ b/a\\b\n@@ -1 +1 @@\n-a\n+b\n", "POSIX"),
])
def test_rejects_unsafe_patch_shapes(tmp_path, patch, message):
    with pytest.raises((ValueError, PermissionError), match=message):
        run(tmp_path, patch)


def test_rejects_sensitive_and_binary(tmp_path):
    (tmp_path / ".git").mkdir(); (tmp_path / ".git" / "config").write_text("x\n")
    sensitive = "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(PermissionError): run(tmp_path, sensitive)
    (tmp_path / "a.txt").write_bytes(b"x\x00\n")
    with pytest.raises(ValueError, match="binary"): run(tmp_path, "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\x00\n+y\n")


def test_rejects_invalid_utf8_and_symlink(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"\xff\n")
    patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(ValueError, match="UTF-8"): run(tmp_path, patch)
    if hasattr(os, "symlink"):
        outside = tmp_path.parent / (tmp_path.name + "-outside"); outside.mkdir()
        (outside / "x.txt").write_text("x\n")
        try: os.symlink(outside, tmp_path / "link", target_is_directory=True)
        except OSError: pytest.skip("symlinks unavailable")
        with pytest.raises(PermissionError):
            run(tmp_path, "--- a/link/x.txt\n+++ b/link/x.txt\n@@ -1 +1 @@\n-x\n+y\n")


def test_caps_duplicates_and_malformed_counts(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x\n")
    duplicate = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n" * 2)
    with pytest.raises(ValueError, match="duplicate"): run(tmp_path, duplicate)
    with pytest.raises(ValueError, match="counts"):
        run(tmp_path, "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1 @@\n-x\n+y\n")
    monkeypatch.setattr(text_patch, "MAX_PATCH_BYTES", 10)
    with pytest.raises(ValueError, match="max patch bytes"): run(tmp_path, MODIFY)


def test_deleted_content_resembling_file_header_is_parsed_as_hunk_body(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("-- option\nkeep\n", encoding="utf-8")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n"
        "--- option\n+enabled\n keep\n"
    )

    run(tmp_path, patch, True)

    assert target.read_text(encoding="utf-8") == "enabled\nkeep\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode and umask semantics")
def test_apply_preserves_modify_mode_and_uses_umask_for_create(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    target.chmod(0o751)
    original_umask = os.umask(0o027)
    try:
        run(tmp_path, MODIFY, True)
        run(tmp_path, CREATE, True)
    finally:
        os.umask(original_umask)

    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == 0o640


def test_create_cleanup_failure_rolls_back_published_target(tmp_path, monkeypatch):
    real_unlink = Path.unlink
    failed = {"value": False}

    def fail_first_stage_cleanup(path, *args, **kwargs):
        if path.name.startswith(".sonder-patch-") and not failed["value"]:
            failed["value"] = True
            raise OSError("injected staging cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_stage_cleanup)
    with pytest.raises(text_patch.TextPatchError) as raised:
        run(tmp_path, CREATE, True)

    assert raised.value.report["transaction"] == "rolled_back"
    assert not (tmp_path / "new.txt").exists()
    assert list(tmp_path.glob(".sonder-patch-*")) == []


def test_public_and_agent_integration(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("one\ntwo\n")
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    output = server._agent_dispatch("text_patch", {
        "root": str(tmp_path), "patch": MODIFY, "apply": False,
        "extra_roots": str(tmp_path), "approval": server._TRUSTED_REPOSITORY_APPROVAL,
    })
    assert json.loads(output)["applied"] is False
    assert "text_patch" in server.tool_manifest()
    assert "- text_patch:" in server._agent_tool_help()
    assert "text_patch" in server._WORK_MUTATION_TOOLS
    assert "text_patch" in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "text_patch" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert server._repository_read_only_error("text_patch", {}, trusted_extra_roots=str(tmp_path)).startswith("ERROR:")


def test_project_scope_checks_every_patch_target(tmp_path):
    project = tmp_path / "project"; project.mkdir()
    scoped = server._project_scope_args("text_patch", {"root": ".", "patch": MODIFY}, str(project))
    assert Path(scoped["root"]) == project
    assert not server._repository_scope_path_error("text_patch", scoped, str(project))
    bad = dict(scoped); bad["patch"] = "--- a/../escape\n+++ b/../escape\n@@ -1 +1 @@\n-a\n+b\n"
    assert "rejected" in server._repository_scope_path_error("text_patch", bad, str(project))


def test_autopilot_accepts_only_host_scoped_text_patch_authority(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    check = server._autopilot_tool_policy({"policy": "workspace", "project": str(project)})
    scoped = server._project_scope_args(
        "text_patch", {"root": ".", "patch": MODIFY, "apply": True}, str(project),
    )

    assert check("text_patch", scoped) == ""
    assert "bypass" in check("text_patch", {
        **scoped, "approval": "model-supplied", "extra_roots": str(project),
    })
    assert "bypass" in check("text_patch", {
        **scoped, "extra_roots": str(tmp_path),
    })


def test_second_publication_failure_rolls_back(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a\n"); (tmp_path / "b.txt").write_text("b\n")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+A\n"
             "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+B\n")
    real_replace, calls = text_patch.os.replace, 0
    def fail_second(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("injected")
        return real_replace(src, dst)
    monkeypatch.setattr(text_patch.os, "replace", fail_second)
    with pytest.raises(text_patch.TextPatchError) as raised: run(tmp_path, patch, True)
    assert raised.value.report["transaction"] == "rolled_back"
    assert (tmp_path / "a.txt").read_text() == "a\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"


def test_concurrent_change_is_preserved_and_prior_publication_rolls_back(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a\n"); (tmp_path / "b.txt").write_text("b\n")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+A\n"
             "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+B\n")
    real_replace, calls = text_patch.os.replace, 0
    def race_after_first(src, dst):
        nonlocal calls
        calls += 1
        result = real_replace(src, dst)
        if calls == 1:
            (tmp_path / "b.txt").write_text("external\n")
        return result
    monkeypatch.setattr(text_patch.os, "replace", race_after_first)
    with pytest.raises(text_patch.TextPatchError) as raised: run(tmp_path, patch, True)
    assert raised.value.report["transaction"] == "rolled_back"
    assert (tmp_path / "a.txt").read_text() == "a\n"
    assert (tmp_path / "b.txt").read_text() == "external\n"


def test_rollback_never_overwrites_concurrent_change_to_published_file(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("a\n"); (tmp_path / "b.txt").write_text("b\n")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-a\n+A\n"
             "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-b\n+B\n")
    real_replace, calls = text_patch.os.replace, 0
    def mutate_then_fail(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("injected")
        result = real_replace(src, dst)
        if calls == 1: (tmp_path / "a.txt").write_text("external\n")
        return result
    monkeypatch.setattr(text_patch.os, "replace", mutate_then_fail)
    with pytest.raises(text_patch.TextPatchError) as raised: run(tmp_path, patch, True)
    assert raised.value.report["transaction"] == "rollback_incomplete"
    assert raised.value.report["rollback_errors"][0]["path"] == "a.txt"
    assert (tmp_path / "a.txt").read_text() == "external\n"
    assert (tmp_path / "b.txt").read_text() == "b\n"
