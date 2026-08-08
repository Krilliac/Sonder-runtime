import os

import pytest

import file_ops


def test_write_read_edit_delete_inside_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    path = "notes/demo.txt"

    wrote = file_ops.write_file(path, "hello world")
    read = file_ops.read_file(path)
    edited = file_ops.edit_file(path, "world", "there")
    dry = file_ops.delete_path(path)
    deleted = file_ops.delete_path(path, dry_run=False, confirm=dry["required_confirm"])

    assert wrote["bytes"] == len("hello world")
    assert read["text"] == "hello world"
    assert edited["replacements"] == 1
    assert dry["deleted"] is False
    assert deleted["deleted"] is True


def test_outside_workspace_rejected_without_bypass(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    with pytest.raises(PermissionError):
        file_ops.read_file(str(outside))


def test_extra_roots_only_apply_with_bypass(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "ok.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    with pytest.raises(PermissionError):
        file_ops.read_file(str(target), extra_roots=str(outside), bypass=False)

    assert file_ops.read_file(str(target), extra_roots=str(outside), bypass=True)["text"] == "ok"


def test_hot_roots_file_allows_explicit_read_root(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    workspace.mkdir()
    repo.mkdir()
    target = repo / "README.md"
    target.write_text("ground truth", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    roots_file = workspace / "trusted-roots.local"
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(roots_file))
    roots_file.write_text(str(repo), encoding="utf-8")

    assert file_ops.read_file(str(target))["text"] == "ground truth"


def test_hot_roots_file_ignores_comments_and_missing_paths(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    roots_file = workspace / "trusted-roots.local"
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(roots_file))
    roots_file.write_text(
        "# approved roots\n\n" + str(tmp_path / "missing") + "\n",
        encoding="utf-8",
    )

    roots = file_ops.allowed_roots()

    assert workspace.resolve() in roots
    assert (tmp_path / "missing").resolve() in roots


def test_find_files_matches_names(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")

    result = file_ops.find_files("*.py")

    assert [r["relative"] for r in result["results"]] == ["a.py"]


def test_recursive_delete_allows_plain_subdirectory(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    target = workspace / "scratch"
    target.mkdir(parents=True)
    state.mkdir()
    (target / "ordinary.txt").write_text("delete me", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: state)

    preview = file_ops.delete_path(str(target), recursive=True)
    result = file_ops.delete_path(
        str(target),
        recursive=True,
        dry_run=False,
        confirm=preview["required_confirm"],
    )

    assert result["deleted"] is True
    assert not target.exists()



def test_repository_read_honors_an_authorized_absolute_root(monkeypatch, tmp_path):
    # Regression (2026-07-13): resolve_repository_read_path rejected EVERY
    # absolute path ("must be relative") and only ever rooted at Sonder's own
    # install dir, so a repository-scoped agent could never read the repo it was
    # pointed at -- while the failure text told the operator to authorize it in
    # file_roots.local, which this resolver never consulted. The delegated
    # repository lane was therefore unusable on any external repo.
    workspace = tmp_path / "sonder"
    repo = tmp_path / "repo"
    (workspace / "sub").mkdir(parents=True)
    (repo / "src").mkdir(parents=True)
    target = repo / "src" / "main.cpp"
    target.write_text("int main() { return 0; }", encoding="utf-8")

    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(repo))

    resolved = file_ops.resolve_repository_read_path(str(target))

    assert resolved == target.resolve()


def test_repository_read_still_rejects_unauthorized_absolute_paths(monkeypatch, tmp_path):
    workspace = tmp_path / "sonder"
    outside = tmp_path / "not-authorized"
    workspace.mkdir(parents=True)
    outside.mkdir(parents=True)
    secret = outside / "creds.txt"
    secret.write_text("token", encoding="utf-8")

    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path(str(secret))


def test_repository_read_rejects_sensitive_dirs_inside_an_authorized_root(monkeypatch, tmp_path):
    # Authorizing a repo must not expose its .git (or .ssh/.aws/...) contents.
    workspace = tmp_path / "sonder"
    repo = tmp_path / "repo"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir(parents=True)
    config = repo / ".git" / "config"
    config.write_text("[core]", encoding="utf-8")

    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(repo))

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path(str(config))


def test_repository_read_rejects_traversal_out_of_the_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "sonder"
    workspace.mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")

    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path("../secret.txt")


@pytest.mark.parametrize(
    "foreign_path",
    [
        "C:\\outside.txt",          # Windows drive-absolute, backslash
        "C:/outside.txt",           # Windows drive-absolute, forward slash
        "D:\\host\\repo\\file.py",  # a different drive letter
        "\\\\server\\share\\x",     # UNC path
    ],
)
def test_repository_read_rejects_foreign_absolute_paths_on_any_host(
    monkeypatch, tmp_path, foreign_path,
):
    # Cross-platform security defect (Linux CI): a Windows drive-absolute or UNC
    # path is NOT absolute under POSIX pathlib, so the resolver silently rebased
    # it under the workspace and accepted it. These forms must be rejected as
    # escaping independent of host OS -- and must never fall back to a workspace
    # read on Windows either.
    workspace = tmp_path / "sonder"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path(foreign_path)

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path(
            foreign_path, allow_workspace_root=True,
        )


def test_repository_read_rejects_posix_absolute_root_form_off_native_host(
    monkeypatch, tmp_path,
):
    # The mirror image of the Windows-on-POSIX defect: a leading-slash POSIX
    # root is caught as absolute-and-escaping on Windows (where it lacks a
    # drive) so it cannot be rebased under the workspace there. On POSIX it is
    # natively absolute and handled by the authorized-root check instead; either
    # way an unauthorized "/etc/passwd" must never resolve to a workspace read.
    workspace = tmp_path / "sonder"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)

    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path("/etc/passwd")


def test_file_ops_errors_carry_a_reason(monkeypatch, tmp_path):
    # Regression (audit): read/write returned a bare "ERROR: <path>" with no
    # cause. The raised exceptions must state the reason.
    import file_ops
    import pytest
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    with pytest.raises(FileNotFoundError, match="file not found"):
        file_ops.read_file("nope.txt")
    file_ops.write_file("exists.txt", "x")
    with pytest.raises(FileExistsError, match="file exists"):
        file_ops.write_file("exists.txt", "y", mode="create")


def _spy_on_read_text(monkeypatch, watched):
    """Record every full-file decode file_ops performs on *watched*."""
    decoded = []
    original = file_ops.Path.read_text

    def _spy(self, *args, **kwargs):
        if os.path.normcase(str(self)) == os.path.normcase(str(watched)):
            decoded.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(file_ops.Path, "read_text", _spy)
    return decoded


def test_delete_preview_does_not_decode_the_file(monkeypatch, tmp_path):
    # Regression: delete_path ran its line-count snapshot BEFORE the dry-run
    # return, decoding the whole target. file_delete defaults to dry_run=True,
    # so previewing a 478 MB DLL inside the always-allowed install root cost
    # ~1.4 GB of peak memory for a call that deletes nothing.
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / "big.bin"
    target.write_bytes(b"\x00" * (file_ops.MAX_SNAPSHOT_BYTES + 1024) + b"\n")
    decoded = _spy_on_read_text(monkeypatch, target)

    preview = file_ops.delete_path("big.bin")

    assert decoded == []
    assert preview["deleted"] is False
    assert preview["would_delete_lines"] == 1


def test_overwrite_of_oversized_file_does_not_decode_it(monkeypatch, tmp_path):
    # Same snapshot, reached through write_file: MAX_WRITE_BYTES bounds the
    # INCOMING content and never bounded the outgoing read of what was there.
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / "big.bin"
    target.write_bytes((b"x" * 63 + b"\n") * 20_000)
    assert target.stat().st_size > file_ops.MAX_SNAPSHOT_BYTES
    decoded = _spy_on_read_text(monkeypatch, target)

    result = file_ops.write_file("big.bin", "small", mode="overwrite")

    assert decoded == []
    assert result["lines_before"] == 20_000
    assert result["lines_after"] == 1


def test_snapshot_line_count_matches_splitlines_for_ordinary_text(monkeypatch, tmp_path):
    # The streaming counter replaced len(text.splitlines()); it must agree with
    # it on the inputs a text file actually produces.
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    for body in ["", "a", "a\n", "a\nb", "a\nb\n", "\n", "a\r\nb\r\n"]:
        target = tmp_path / "sample.txt"
        target.write_text(body, encoding="utf-8", newline="")
        assert file_ops._line_count_on_disk(target) == file_ops._line_count(body), body


def test_is_inside_ignores_case_where_the_filesystem_does(tmp_path):
    # Regression: _is_inside normpath'd but did not normcase, and commonpath is
    # case-sensitive, so on Windows a contained path spelled with a different
    # drive/letter case was refused as an escape.
    root = tmp_path / "Work"
    root.mkdir()
    inside = root / "sub" / "file.txt"

    assert file_ops._is_inside(inside, root)
    assert file_ops._is_inside(
        file_ops.Path(str(inside).upper()), file_ops.Path(str(root).lower())
    ) is (os.path.normcase("A") == os.path.normcase("a"))
    assert not file_ops._is_inside(tmp_path / "Other" / "file.txt", root)
