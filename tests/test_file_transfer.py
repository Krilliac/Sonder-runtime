import errno
import hashlib
import os

import pytest

import file_ops


@pytest.fixture()
def workspace(monkeypatch, tmp_path):
    root = tmp_path / "workspace"
    state = tmp_path / "state"
    root.mkdir()
    state.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: state)
    return root


def test_copy_is_binary_safe_atomic_and_deterministic(workspace, monkeypatch):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    payload = bytes(range(256)) * 8 + b"\x00\xff\xfe"
    source.write_bytes(payload)
    publications = []
    real_link = file_ops.os.link

    def capture_link(src, dst, **kwargs):
        publications.append((file_ops.Path(src), file_ops.Path(dst), kwargs))
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(file_ops.os, "link", capture_link)
    result = file_ops.copy_file("source.bin", "out.bin")

    assert source.read_bytes() == payload
    assert destination.read_bytes() == payload
    assert result == {
        "action": "copy",
        "bytes": len(payload),
        "destination": str(destination),
        "overwrite": False,
        "path": str(destination),
        "replaced": False,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source": str(source),
    }
    temp, final, link_kwargs = publications[-1]
    assert temp.name.startswith(".out.bin.sonder-transfer-")
    assert final.name == destination.name
    if temp.is_absolute():
        assert temp.parent == destination.parent
        assert final == destination
    else:
        assert link_kwargs["src_dir_fd"] == link_kwargs["dst_dir_fd"]
    assert not list(workspace.glob(".*.sonder-transfer-*"))


def test_copy_refuses_overwrite_by_default_and_preserves_destination(workspace):
    (workspace / "source.bin").write_bytes(b"new")
    destination = workspace / "out.bin"
    destination.write_bytes(b"old")
    with pytest.raises(FileExistsError, match="overwrite=true"):
        file_ops.copy_file("source.bin", "out.bin")
    assert destination.read_bytes() == b"old"

    result = file_ops.copy_file("source.bin", "out.bin", overwrite=True)
    assert result["replaced"] is True
    assert destination.read_bytes() == b"new"


@pytest.mark.parametrize("operation", [file_ops.copy_file, file_ops.move_file])
@pytest.mark.parametrize("overwrite", [1, 0, "true", "false", None])
def test_transfer_requires_strict_boolean_overwrite(workspace, operation, overwrite):
    (workspace / "source.bin").write_bytes(b"source")
    with pytest.raises(ValueError, match="overwrite must be a boolean"):
        operation("source.bin", "out.bin", overwrite=overwrite)


def test_copy_no_overwrite_publication_never_clobbers_concurrent_file(
    workspace, monkeypatch
):
    (workspace / "source.bin").write_bytes(b"source")
    destination = workspace / "out.bin"
    real_link = file_ops.os.link

    def install_competitor_then_link(src, dst, **kwargs):
        if file_ops.Path(dst).name == destination.name:
            destination.write_bytes(b"competitor")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(file_ops.os, "link", install_competitor_then_link)
    with pytest.raises(FileExistsError):
        file_ops.copy_file("source.bin", "out.bin")
    assert destination.read_bytes() == b"competitor"
    assert not list(workspace.glob(".*.sonder-transfer-*"))


def test_copy_rejects_source_replaced_after_validation(workspace, monkeypatch):
    source = workspace / "source.bin"
    replacement = workspace / "replacement.bin"
    source.write_bytes(b"validated")
    replacement.write_bytes(b"replacement")
    swapped = False

    def perform_swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            replacement.replace(source)

    if file_ops.os.name == "nt":
        real_open = file_ops._windows_open_source

        def swap_before_open(path):
            if file_ops.Path(path) == source:
                perform_swap()
            return real_open(path)

        monkeypatch.setattr(file_ops, "_windows_open_source", swap_before_open)
    else:
        real_open = file_ops.os.open

        def swap_before_open(path, flags, *args, **kwargs):
            if file_ops.Path(path).name == source.name:
                perform_swap()
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(file_ops.os, "open", swap_before_open)
    with pytest.raises(PermissionError, match="source changed before open"):
        file_ops.copy_file("source.bin", "out.bin")
    assert not (workspace / "out.bin").exists()


def test_copy_rejects_destination_parent_rebound_after_temp_creation(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination_parent = workspace / "output"
    displaced_parent = workspace / "displaced-output"
    source.write_bytes(b"payload")
    destination_parent.mkdir()
    real_publish = file_ops._DirectoryAnchor.publish

    def rebind_before_publish(anchor, *args, **kwargs):
        destination_parent.replace(displaced_parent)
        destination_parent.mkdir()
        return real_publish(anchor, *args, **kwargs)

    monkeypatch.setattr(file_ops._DirectoryAnchor, "publish", rebind_before_publish)
    with pytest.raises(PermissionError):
        file_ops.copy_file("source.bin", "output/out.bin")
    assert not (destination_parent / "out.bin").exists()


def test_copy_enforces_hard_size_cap_before_creating_destination(
    workspace, monkeypatch
):
    monkeypatch.setattr(file_ops, "MAX_TRANSFER_BYTES", 3)
    (workspace / "source.bin").write_bytes(b"four")
    with pytest.raises(ValueError, match="max transfer bytes"):
        file_ops.copy_file("source.bin", "out.bin")
    assert not (workspace / "out.bin").exists()


def test_move_same_volume_removes_source(workspace):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"payload")
    result = file_ops.move_file("source.bin", "out.bin")
    assert result["method"] == "copy-delete"
    assert not source.exists()
    assert destination.read_bytes() == b"payload"


def test_move_uses_cross_volume_safe_atomic_copy_delete(workspace):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"cross-volume")

    result = file_ops.move_file("source.bin", "out.bin")
    assert result["method"] == "copy-delete"
    assert not source.exists()
    assert destination.read_bytes() == b"cross-volume"


def test_move_rolls_back_if_source_grows_during_destination_publication(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"initial")
    real_chmod = file_ops._DirectoryAnchor.chmod

    def chmod_then_grow(anchor, *args, **kwargs):
        real_chmod(anchor, *args, **kwargs)
        with source.open("ab") as stream:
            stream.write(b"-grew")

    monkeypatch.setattr(file_ops._DirectoryAnchor, "chmod", chmod_then_grow)
    with pytest.raises((PermissionError, OSError)):
        file_ops.move_file("source.bin", "out.bin")
    assert source.read_bytes() in {b"initial", b"initial-grew"}
    assert not destination.exists()


def test_move_fallback_rolls_back_new_destination_when_source_delete_fails(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"keep source")
    real_unlink = file_ops._DirectoryAnchor.unlink

    def fail_source_unlink(anchor, name):
        if anchor.path == source.parent and name == source.name:
            raise OSError(errno.EACCES, "busy")
        return real_unlink(anchor, name)

    monkeypatch.setattr(file_ops._DirectoryAnchor, "unlink", fail_source_unlink)
    with pytest.raises(OSError, match="busy"):
        file_ops.move_file("source.bin", "out.bin")
    assert source.read_bytes() == b"keep source"
    assert not destination.exists()


def test_move_overwrite_fallback_restores_old_destination_on_failure(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    real_unlink = file_ops._DirectoryAnchor.unlink

    def fail_source_unlink(anchor, name):
        if anchor.path == source.parent and name == source.name:
            raise OSError(errno.EACCES, "busy")
        return real_unlink(anchor, name)

    monkeypatch.setattr(file_ops._DirectoryAnchor, "unlink", fail_source_unlink)
    with pytest.raises(OSError, match="busy"):
        file_ops.move_file("source.bin", "out.bin", overwrite=True)
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"
    assert not list(workspace.glob(".*.sonder-move-backup-*"))


def test_move_rolls_back_when_replaced_destination_backup_cannot_be_removed(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    real_unlink = file_ops._DirectoryAnchor.unlink
    backup_unlinks = 0

    def fail_committed_backup_unlink(anchor, name):
        nonlocal backup_unlinks
        if ".sonder-move-backup-" in name:
            backup_unlinks += 1
            if backup_unlinks == 1:
                raise OSError(errno.EACCES, "backup busy")
        return real_unlink(anchor, name)

    monkeypatch.setattr(
        file_ops._DirectoryAnchor, "unlink", fail_committed_backup_unlink,
    )
    with pytest.raises(OSError, match="backup busy"):
        file_ops.move_file("source.bin", "out.bin", overwrite=True)
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"
    assert not list(workspace.glob(".*.sonder-move-backup-*"))


@pytest.mark.parametrize("operation", [file_ops.copy_file, file_ops.move_file])
def test_transfer_rejects_directories_git_sensitive_and_outside(
    workspace, tmp_path, operation
):
    (workspace / "folder").mkdir()
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("secret", encoding="utf-8")
    (workspace / ".ssh").mkdir()
    (workspace / ".ssh" / "config").write_text("secret", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=x", encoding="utf-8")
    (workspace / "source.bin").write_bytes(b"ok")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    with pytest.raises(ValueError, match="regular file"):
        operation("folder", "out.bin")
    with pytest.raises(PermissionError, match=".git"):
        operation(".git/config", "out.bin")
    with pytest.raises(PermissionError, match=".ssh"):
        operation(".ssh/config", "out.bin")
    with pytest.raises(PermissionError, match="sensitive"):
        operation(".env", "out.bin")
    with pytest.raises(PermissionError, match="outside allowed roots"):
        operation(str(outside), "out.bin")
    with pytest.raises(PermissionError, match="foreign absolute|outside allowed roots"):
        operation("Z:\\foreign.bin", "out.bin")
    with pytest.raises(PermissionError, match=".git"):
        operation("source.bin", ".git/out.bin")


def test_transfer_rejects_symlink_and_destination_parent_symlink(workspace):
    source = workspace / "source.bin"
    source.write_bytes(b"payload")
    real_dir = workspace / "real"
    real_dir.mkdir()
    try:
        os.symlink(source, workspace / "source-link.bin")
        os.symlink(real_dir, workspace / "dir-link", target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")
    with pytest.raises(PermissionError, match="symlink or junction"):
        file_ops.copy_file("source-link.bin", "out.bin")
    with pytest.raises(PermissionError, match="symlink or junction"):
        file_ops.copy_file("source.bin", "dir-link/out.bin")
