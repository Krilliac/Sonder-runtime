"""Single-file mutation guards for sensitive directories and credential stores (G0648).

``write_file``, ``edit_file``, ``make_directory``, and ``delete_path`` go
through ``_require_mutation_access``. When an operator-configured root holds a
profile/home-like directory (via ``SONDER_FILE_ROOTS``, ``file_roots.local``,
or an approved one-shot ``extra_roots``), token-less mutations into
``SENSITIVE_READ_DIRECTORIES`` (``.ssh``, ``.aws``, ``.azure``, ``.gnupg``,
``.kube``, ``.gcloud``) or any ``credential_read_component`` store must be
refused relative to the containing root, while an operator root that names
``.ssh`` itself stays writable.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sonder_runtime.adapters.filesystem import file_ops


@pytest.fixture()
def home_root(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    state_home = tmp_path / "state_home"
    profile = tmp_path / "profile"
    ws.mkdir()
    state_home.mkdir()
    profile.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: state_home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(state_home / "file_roots.local"))
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(profile))
    return profile


def _assert_path_before_phrase(message: str, target: Path) -> None:
    resolved_text = str(target.resolve())
    phrase = "without an authenticated developer token"
    assert resolved_text in message, message
    assert phrase in message, message
    assert message.index(resolved_text) < message.index(phrase), message


@pytest.mark.parametrize("relative", [
    ".ssh/config",
    ".ssh/authorized_keys",
    ".kube/config",
    ".aws/config",
    ".azure/config",
    ".gnupg/gpg.conf",
    ".gcloud/config",
    ".SSH/config",
    "nested/.ssh/config",
])
def test_write_file_into_sensitive_dirs_is_refused_and_allowed_with_token(home_root, relative):
    target = home_root / relative
    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.write_file(str(target), "Host *\n", mode="create")
    _assert_path_before_phrase(str(info.value), target)
    assert not target.exists()

    out = file_ops.write_file(str(target), "Host *\n", mode="create", developer_authorized=True)
    assert out["bytes"] > 0
    assert target.read_text(encoding="utf-8") == "Host *\n"


def test_edit_file_into_ssh_config_is_refused_by_mutation_guard(home_root, monkeypatch):
    target = home_root / ".ssh" / "config"
    target.parent.mkdir(parents=True)
    target.write_text("Host old\n", encoding="utf-8")

    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.edit_file(str(target), "old", "new")
    _assert_path_before_phrase(str(info.value), target)
    assert target.read_text(encoding="utf-8") == "Host old\n"

    # A developer token clears the mutation guard; edit_file still respects the
    # separate credential-store read guard unless the root names .ssh itself.
    file_ops._require_mutation_access(target.resolve(), developer_authorized=True)
    with pytest.raises(PermissionError, match="refusing to read.*credential store"):
        file_ops.edit_file(str(target), "old", "new", developer_authorized=True)

    monkeypatch.setenv("SONDER_FILE_ROOTS", str(target.parent))
    edited = file_ops.edit_file(str(target), "old", "new", developer_authorized=True)
    assert edited["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "Host new\n"


@pytest.mark.parametrize("relative", [
    ".ssh",
    ".ssh/sockets",
    ".kube/cache",
])
def test_make_directory_into_sensitive_dir_is_refused_and_allowed_with_token(home_root, relative):
    target = home_root / relative
    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.make_directory(str(target))
    _assert_path_before_phrase(str(info.value), target)
    assert not target.exists()

    out = file_ops.make_directory(str(target), developer_authorized=True)
    assert out["created"] is True
    assert target.is_dir()


def test_delete_path_into_ssh_config_is_refused_and_allowed_with_token(home_root):
    target = home_root / ".ssh" / "config"
    target.parent.mkdir(parents=True)
    target.write_text("Host example\n", encoding="utf-8")
    confirm = f"DELETE {target.resolve()}"

    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.delete_path(str(target), dry_run=True)
    _assert_path_before_phrase(str(info.value), target)

    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.delete_path(str(target), dry_run=False, confirm=confirm)
    _assert_path_before_phrase(str(info.value), target)
    assert target.is_file()

    out = file_ops.delete_path(
        str(target), dry_run=False, confirm=confirm, developer_authorized=True,
    )
    assert out["deleted"] is True
    assert not target.exists()


def test_recursive_delete_of_parent_containing_ssh_dir_is_refused_without_token(home_root):
    parent = home_root / "sub"
    target = parent / ".ssh" / "config"
    target.parent.mkdir(parents=True)
    target.write_text("Host example\n", encoding="utf-8")
    confirm = f"DELETE {parent.resolve()}"

    with pytest.raises(PermissionError, match="without an authenticated developer token"):
        file_ops.delete_path(str(parent), recursive=True, dry_run=False, confirm=confirm)
    assert target.is_file()

    out = file_ops.delete_path(
        str(parent), recursive=True, dry_run=False, confirm=confirm, developer_authorized=True,
    )
    assert out["deleted"] is True
    assert not parent.exists()


@pytest.mark.parametrize("relative", [
    ".docker/config.json",
    ".config/gh/hosts.yml",
    ".config/gcloud/credentials.db",
    ".cargo/credentials.toml",
])
def test_credential_store_paths_are_refused_on_mutation_and_allowed_with_token(home_root, relative):
    target = home_root / relative
    with pytest.raises(PermissionError, match="refusing to mutate") as info:
        file_ops.write_file(str(target), "data\n", mode="create")
    _assert_path_before_phrase(str(info.value), target)
    assert not target.exists()

    out = file_ops.write_file(str(target), "data\n", mode="create", developer_authorized=True)
    assert out["bytes"] > 0


def test_operator_root_naming_ssh_folder_itself_stays_writable(home_root, monkeypatch):
    ssh_dir = home_root / ".ssh"
    ssh_dir.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(ssh_dir))

    cfg = ssh_dir / "config"
    assert file_ops.write_file(str(cfg), "Host dev\n")["bytes"] > 0
    assert file_ops.edit_file(str(cfg), "dev", "prod")["replacements"] == 1
    assert cfg.read_text(encoding="utf-8") == "Host prod\n"
    sub = ssh_dir / "control"
    assert file_ops.make_directory(str(sub))["created"] is True
    assert file_ops.delete_path(
        str(cfg), dry_run=False, confirm=f"DELETE {cfg.resolve()}",
    )["deleted"] is True

    # When both the home-like root and the explicit .ssh root are configured,
    # .ssh stays writable while another sensitive directory (.kube) is refused.
    monkeypatch.setenv("SONDER_FILE_ROOTS", f"{home_root}{os.pathsep}{ssh_dir}")
    assert file_ops.write_file(str(cfg), "Host ok\n")["bytes"] > 0
    with pytest.raises(PermissionError, match="refusing to mutate"):
        file_ops.write_file(str(home_root / ".kube" / "config"), "clusters: []\n")


def test_file_roots_local_and_one_shot_extra_roots_still_refuse_sensitive_dirs(home_root, monkeypatch):
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    roots_file = Path(file_ops.roots_file_path())
    roots_file.write_text(str(home_root) + "\n", encoding="utf-8")

    auth_keys = home_root / ".ssh" / "authorized_keys"
    with pytest.raises(PermissionError, match="refusing to mutate"):
        file_ops.write_file(str(auth_keys), "ssh-ed25519 AAAA\n")
    assert not auth_keys.exists()

    roots_file.unlink()
    kube_cfg = home_root / ".kube" / "config"
    with pytest.raises(PermissionError, match="refusing to mutate"):
        file_ops.write_file(
            str(kube_cfg), "clusters: []\n", extra_roots=str(home_root), bypass=True,
        )
    assert not kube_cfg.exists()


@pytest.mark.parametrize("relative", [
    ".ssh/authorized_keys",
    ".ssh/config",
    ".aws/config",
    ".kube/config",
    ".gnupg/gpg.conf",
    ".gcloud/config",
    ".docker/config.json",
    ".config/gh/hosts.yml",
    ".config/gcloud/credentials.db",
])
def test_patch_refusal_blocks_sensitive_dirs_and_credential_stores(home_root, relative):
    from sonder_runtime.adapters import git_change_targets

    assert git_change_targets.patch_refusal("apply_patch", home_root, home_root, [relative]), relative


def test_archive_extract_blocks_gcloud_and_direct_credential_store_destinations(home_root):
    import zipfile
    from sonder_runtime.adapters.inspection import archive_tools

    cases = [
        ("drop_gcloud", ".gcloud/config"),
        (".docker", "config.json"),
        (".config", "gh/hosts.yml"),
        (".config_gcloud", ".gcloud/credentials.db"),
    ]
    for idx, (dest_rel, member) in enumerate(cases):
        archive = home_root / f"drop_{idx}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(member, "secret\n")
        dest = home_root / dest_rel
        with pytest.raises(PermissionError, match="refusing to mutate"):
            archive_tools.extract_archive(str(archive), str(dest))
        assert not dest.exists()

    # Direct .config destination with gcloud/credentials.db is also refused.
    archive = home_root / "drop_cfg_gcloud.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("gcloud/credentials.db", "secret\n")
    with pytest.raises(PermissionError, match="refusing to mutate"):
        archive_tools.extract_archive(str(archive), str(home_root / ".config"))
    assert not (home_root / ".config").exists()


def test_batch_write_preflight_rejects_credential_store_without_writing_or_reading_size(home_root):
    docker_cfg = home_root / ".docker" / "config.json"
    docker_cfg.parent.mkdir(parents=True)
    docker_cfg.write_text('{"auths":{"secret":"1234567890"}}\n', encoding="utf-8")
    benign = home_root / "benign_batch.txt"
    with pytest.raises(file_ops.BatchWriteError) as info:
        file_ops.batch_write_files([
            {"path": str(benign), "content": "hi\n", "mode": "create"},
            {"path": str(docker_cfg), "content": "{}\n", "mode": "overwrite"},
        ])
    assert info.value.report["transaction"] == "not_started"
    assert info.value.report["snapshot_bytes"] == 0
    assert not benign.exists()


def test_protected_write_scope_refuses_directory_containing_ssh(home_root):
    from sonder_runtime.adapters import protected_write_scope

    sub = home_root / "fmt_sub"
    (sub / ".ssh").mkdir(parents=True)
    (sub / ".ssh" / "config").write_text("Host *\n", encoding="utf-8")
    assert protected_write_scope.scope_refusal("format_code", home_root, sub)
