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
    replacements = []
    real_replace = file_ops.os.replace

    def capture_replace(src, dst):
        replacements.append((file_ops.Path(src), file_ops.Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(file_ops.os, "replace", capture_replace)
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
    temp, final = replacements[-1]
    assert temp.parent == destination.parent
    assert final == destination
    assert not list(workspace.glob(".*.sonder-copy-*"))


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
    assert result["method"] == "link"
    assert not source.exists()
    assert destination.read_bytes() == b"payload"


def test_move_cross_volume_falls_back_to_atomic_copy_delete(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"cross-volume")

    def cross_volume(*args, **kwargs):
        raise OSError(errno.EXDEV, "different devices")

    monkeypatch.setattr(file_ops.os, "link", cross_volume)
    result = file_ops.move_file("source.bin", "out.bin")
    assert result["method"] == "copy-delete"
    assert not source.exists()
    assert destination.read_bytes() == b"cross-volume"


def test_move_fallback_rolls_back_new_destination_when_source_delete_fails(
    workspace, monkeypatch
):
    source = workspace / "source.bin"
    destination = workspace / "out.bin"
    source.write_bytes(b"keep source")
    monkeypatch.setattr(
        file_ops.os,
        "link",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross")),
    )
    real_unlink = file_ops.Path.unlink

    def fail_source_unlink(self, *args, **kwargs):
        if self == source:
            raise OSError(errno.EACCES, "busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(file_ops.Path, "unlink", fail_source_unlink)
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
    real_replace = file_ops.os.replace

    def cross_volume_source(src, dst):
        if file_ops.Path(src) == source and file_ops.Path(dst) == destination:
            raise OSError(errno.EXDEV, "cross")
        return real_replace(src, dst)

    real_unlink = file_ops.Path.unlink

    def fail_source_unlink(self, *args, **kwargs):
        if self == source:
            raise OSError(errno.EACCES, "busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(file_ops.os, "replace", cross_volume_source)
    monkeypatch.setattr(file_ops.Path, "unlink", fail_source_unlink)
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
    real_unlink = file_ops.Path.unlink
    backup_unlinks = 0

    def fail_committed_backup_unlink(self, *args, **kwargs):
        nonlocal backup_unlinks
        if ".sonder-move-backup-" in self.name:
            backup_unlinks += 1
            if backup_unlinks == 2:
                raise OSError(errno.EACCES, "backup busy")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(file_ops.Path, "unlink", fail_committed_backup_unlink)
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
