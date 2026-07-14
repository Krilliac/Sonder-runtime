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


def test_file_ops_errors_carry_a_reason(monkeypatch, tmp_path):
    # Regression (audit): read/write returned a bare "ERROR: <path>" with no
    # cause. The raised exceptions must state the reason.
    import file_ops, pytest
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    with pytest.raises(FileNotFoundError, match="file not found"):
        file_ops.read_file("nope.txt")
    file_ops.write_file("exists.txt", "x")
    with pytest.raises(FileExistsError, match="file exists"):
        file_ops.write_file("exists.txt", "y", mode="create")
