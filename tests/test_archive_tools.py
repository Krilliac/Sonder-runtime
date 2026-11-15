from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import zipfile

import pytest

from sonder_runtime.adapters.inspection import archive_tools
import sonder_runtime.adapters.filesystem.file_ops as file_ops
import server


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    return root


def _zip(path, rows):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in rows:
            archive.writestr(name, payload)


def _tar(path, rows):
    with tarfile.open(path, "w") as archive:
        for name, payload in rows:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize("name", [
    "../escape.txt", "/absolute.txt", "C:/drive.txt", "//server/share.txt",
    "a/../../escape.txt", "NUL.txt", "a:b.txt",
])
def test_zip_slip_and_cross_platform_paths_are_rejected(workspace, name):
    source = workspace / "bad.zip"
    _zip(source, [(name, b"bad")])

    data = archive_tools.list_archive("bad.zip")

    assert data["valid"] is False
    assert not (workspace.parent / "escape.txt").exists()


def test_backslash_member_syntax_is_rejected_independent_of_host_normalization():
    with pytest.raises(archive_tools.ArchiveRejected, match="backslash"):
        archive_tools._portable_member_path(
            "a\\windows.txt", is_directory=False, max_depth=10,
        )


def test_tar_slip_is_rejected_before_any_destination_exists(workspace):
    source = workspace / "bad.tar"
    _tar(source, [("safe.bin", b"safe"), ("../escape.bin", b"bad")])

    with pytest.raises(archive_tools.ArchiveRejected):
        archive_tools.extract_archive("bad.tar", "result")

    assert not (workspace / "result").exists()
    assert list(workspace.glob(".sonder-archive-*")) == []


def test_tar_directory_payload_is_rejected(workspace):
    source = workspace / "directory-payload.tar"
    with tarfile.open(source, "w") as archive:
        info = tarfile.TarInfo("payload/")
        info.type = tarfile.DIRTYPE
        info.size = 32
        archive.addfile(info, io.BytesIO(b"x" * 32))

    data = archive_tools.list_archive(str(source))

    assert data["valid"] is False
    assert "nonzero payload" in data["errors"][0]


def test_binary_zip_extracts_transactionally_and_deterministically(workspace):
    payload = bytes(range(256)) + b"\x00\xff"
    _zip(workspace / "bundle.zip", [
        ("z.bin", payload), ("nested/a.txt", b"alpha"),
        ("unicode/\u96ea.txt", "caf\u00e9".encode("utf-8")),
    ])

    listed = archive_tools.list_archive("bundle.zip")
    extracted = archive_tools.extract_archive("bundle.zip", "output")

    assert listed["valid"] is True
    assert [row["path"] for row in listed["entries"]] == [
        "nested/a.txt", "unicode/\u96ea.txt", "z.bin",
    ]
    assert extracted["validation_passed"] is True
    assert (workspace / "output" / "z.bin").read_bytes() == payload
    assert (workspace / "output" / "nested" / "a.txt").read_bytes() == b"alpha"
    assert (workspace / "output" / "unicode" / "\u96ea.txt").read_text(encoding="utf-8") == "caf\u00e9"


def test_existing_destination_is_never_overwritten(workspace):
    _zip(workspace / "bundle.zip", [("a.txt", b"new")])
    destination = workspace / "output"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        archive_tools.extract_archive("bundle.zip", "output")

    assert (destination / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (destination / "a.txt").exists()


def test_extraction_failure_rolls_back_staging_directory(workspace, monkeypatch):
    _zip(workspace / "bundle.zip", [("a.txt", b"payload")])

    def fail_after_partial(_source, stage, _plan, _deadline):
        (stage / "partial.bin").write_bytes(b"partial")
        raise OSError("simulated extraction failure")

    monkeypatch.setattr(archive_tools, "_extract_zip", fail_after_partial)

    with pytest.raises(OSError, match="simulated"):
        archive_tools.extract_archive("bundle.zip", "output")

    assert not (workspace / "output").exists()
    assert list(workspace.glob(".sonder-archive-*")) == []


def test_source_mutation_after_prevalidation_rolls_back(workspace, monkeypatch):
    source = workspace / "bundle.zip"
    _zip(source, [("a.txt", b"payload")])
    original = archive_tools._extract_zip

    def mutate_after_extract(source_path, stage, plan, deadline):
        original(source_path, stage, plan, deadline)
        with source_path.open("ab") as stream:
            stream.write(b"changed")

    monkeypatch.setattr(archive_tools, "_extract_zip", mutate_after_extract)

    with pytest.raises(archive_tools.ArchiveRejected, match="changed"):
        archive_tools.extract_archive("bundle.zip", "output")

    assert not (workspace / "output").exists()
    assert list(workspace.glob(".sonder-archive-*")) == []


def test_same_size_same_mtime_source_mutation_is_detected(workspace, monkeypatch):
    source = workspace / "bundle.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("a.txt", b"safe")
    original_stat = source.stat()
    original = archive_tools._extract_zip

    def mutate_without_metadata_change(source_path, stage, plan, deadline):
        original(source_path, stage, plan, deadline)
        changed = source_path.read_bytes().replace(b"safe", b"evil")
        source_path.write_bytes(changed)
        os.utime(
            source_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

    monkeypatch.setattr(archive_tools, "_extract_zip", mutate_without_metadata_change)

    with pytest.raises(archive_tools.ArchiveRejected, match="changed"):
        archive_tools.extract_archive("bundle.zip", "output")

    assert not (workspace / "output").exists()
    assert list(workspace.glob(".sonder-archive-*")) == []


@pytest.mark.parametrize("rows, message", [
    ([("a.txt", b"1"), ("a.txt", b"2")], "duplicate"),
    ([("Readme.txt", b"1"), ("README.TXT", b"2")], "case-colliding"),
    ([("Folder/a.txt", b"1"), ("folder/b.txt", b"2")], "case-colliding"),
    ([("inner.zip", b"not recursively extracted")], "nested archive"),
    ([(".env", b"SECRET=value")], "sensitive"),
])
def test_collisions_nested_archives_and_sensitive_members_fail_closed(
    workspace, rows, message,
):
    _zip(workspace / "bad.zip", rows)

    data = archive_tools.list_archive("bad.zip")

    assert data["valid"] is False
    assert message in data["errors"][0]


def test_zip_symlink_and_encrypted_metadata_are_rejected(workspace):
    source = workspace / "links.zip"
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(info, "target")
    assert "symlink" in archive_tools.list_archive("links.zip")["errors"][0]

    encrypted = zipfile.ZipInfo("public.txt")
    encrypted.flag_bits |= 0x1
    with pytest.raises(archive_tools.ArchiveRejected, match="encrypted"):
        archive_tools._zip_entry(encrypted, archive_tools._limits(None, None, None, None, None, None, None))


def test_tar_links_and_special_entries_are_rejected(workspace):
    source = workspace / "links.tar"
    with tarfile.open(source, "w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "target"
        archive.addfile(info)

    data = archive_tools.list_archive("links.tar")

    assert data["valid"] is False
    assert "symlink/hardlink" in data["errors"][0]


@pytest.mark.parametrize("entry_type", [tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_tar_hardlinks_and_devices_are_rejected(workspace, entry_type):
    source = workspace / "special.tar"
    with tarfile.open(source, "w") as archive:
        info = tarfile.TarInfo("special")
        info.type = entry_type
        if entry_type == tarfile.LNKTYPE:
            info.linkname = "target"
        archive.addfile(info)

    assert archive_tools.list_archive("special.tar")["valid"] is False


def test_zip_device_metadata_is_rejected(workspace):
    source = workspace / "device.zip"
    info = zipfile.ZipInfo("device")
    info.create_system = 3
    info.external_attr = (stat.S_IFCHR | 0o600) << 16
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(info, b"")

    data = archive_tools.list_archive("device.zip")
    assert data["valid"] is False
    assert "special/device" in data["errors"][0]


def test_entry_byte_ratio_result_and_depth_caps_are_explicit(workspace):
    _zip(workspace / "bomb.zip", [("huge.txt", b"0" * 100_000)])
    assert "compression-ratio" in archive_tools.list_archive(
        "bomb.zip", max_ratio=2,
    )["errors"][0]

    _zip(workspace / "many.zip", [("a.txt", b"a"), ("b.txt", b"b")])
    listed = archive_tools.list_archive("many.zip", max_results=1)
    assert listed["valid"] is True
    assert listed["truncated"] is True
    assert listed["omitted_results"] == 1

    _zip(workspace / "deep.zip", [("a/b/c.txt", b"x")])
    assert "path-depth" in archive_tools.list_archive(
        "deep.zip", max_path_depth=2,
    )["errors"][0]

    assert "per-file" in archive_tools.list_archive(
        "bomb.zip", max_file_bytes=10,
    )["errors"][0]
    assert "aggregate" in archive_tools.list_archive(
        "many.zip", max_total_bytes=1,
    )["errors"][0]
    assert "entry ceiling" in archive_tools.list_archive(
        "many.zip", max_entries=1,
    )["errors"][0]


def test_source_symlink_escape_and_destination_junction_are_rejected(
    workspace, tmp_path,
):
    outside = tmp_path / "outside.zip"
    _zip(outside, [("a.txt", b"x")])
    try:
        os.symlink(outside, workspace / "linked.zip")
    except (OSError, NotImplementedError) as exc:
        pytest.skip("symlink creation unavailable: %s" % exc)

    with pytest.raises(PermissionError):
        archive_tools.list_archive("linked.zip")


def test_server_registration_dispatch_and_project_scope(workspace, tmp_path):
    _zip(workspace / "bundle.zip", [("a.txt", b"x")])
    assert "archive_list/archive_extract" in server.tool_manifest()
    assert "archive_list" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "archive_extract" in server._WORK_MUTATION_TOOLS
    assert "archive_list" in server._AUTOPILOT_OBSERVE_TOOLS
    assert "archive_extract" in server._AUTOPILOT_WORKSPACE_TOOLS

    listed = json.loads(server._agent_dispatch(
        "archive_list", {"path": "bundle.zip"}, read_only=True,
        repository_extra_roots=str(workspace),
    ))
    assert listed["valid"] is True

    outside = tmp_path / "outside"
    outside.mkdir()
    error = server._repository_scope_path_error(
        "archive_extract",
        {"source": "bundle.zip", "destination": str(outside / "output")},
        str(workspace),
    )
    assert "outside the host-selected project root" in error


def test_direct_mcp_extra_roots_require_authorization(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _zip(outside / "bundle.zip", [("a.txt", b"x")])

    result = server.archive_list(
        str(outside / "bundle.zip"), extra_roots=str(outside),
    )

    assert result.startswith("ERROR:")
    assert "extra_roots requires" in result


def test_no_replace_promotion_preserves_competing_destination(workspace):
    stage = workspace / "stage"
    destination = workspace / "destination"
    stage.mkdir()
    (stage / "ours.txt").write_text("ours", encoding="utf-8")
    destination.mkdir()
    (destination / "theirs.txt").write_text("theirs", encoding="utf-8")

    with pytest.raises(FileExistsError):
        archive_tools._promote_no_replace(stage, destination)

    assert (destination / "theirs.txt").read_text(encoding="utf-8") == "theirs"
    assert stage.exists()


def test_archive_extract_is_self_validating_for_agent_bookkeeping(workspace):
    args = {"source": str(workspace / "bundle.zip"), "destination": str(workspace / "out")}
    mutations = [{"tool": "archive_extract", "path": server._agent_normalized_path(args["destination"])}]
    observation = json.dumps({"ok": True, "validation_passed": True})
    assert server._agent_validation_covers("archive_extract", args, mutations, observation)


@pytest.mark.parametrize("member", [
    ".npmrc", "kubeconfig", ".htpasswd", "pkg/.npmrc", "vault/passwords.kdbx",
    ".config/gcloud/credentials.db", ".config/gh/hosts.yml",
])
def test_tarball_with_token_store_names_extracts_but_reads_stay_refused(workspace, member):
    # Policy: ordinary repositories ship .npmrc, so extraction is not refused
    # for the token-store names added to the read guard; reading them still is.
    _tar(workspace / "repo.tar", [("README.md", b"hi"), (member, b"TOKEN-MATERIAL")])

    listed = archive_tools.list_archive("repo.tar")
    extracted = archive_tools.extract_archive("repo.tar", "output")

    assert listed["valid"] is True
    assert extracted["validation_passed"] is True
    assert (workspace / "output" / member).read_bytes() == b"TOKEN-MATERIAL"
    assert file_ops.read_file("output/README.md")["text"] == "hi"
    with pytest.raises(PermissionError, match="credential store"):
        file_ops.read_file("output/" + member)


@pytest.mark.parametrize("name", [
    ".env", "id_rsa", ".netrc", ".pgpass", "key.pem",
    "application_default_credentials.json", "credentials.tfrc.json",
    ".env.kdbx", ".env.prod.kdbx",
])
def test_extraction_still_refuses_what_it_refused_before(workspace, name):
    _zip(workspace / "bad.zip", [(name, b"SECRET")])
    data = archive_tools.list_archive("bad.zip")
    assert data["valid"] is False
    assert "sensitive" in data["errors"][0]


def _frozen_pre_branch_refuses(name: str) -> bool:
    """Frozen copy of the extraction secret-name rules as of d17a377d."""
    from pathlib import PurePosixPath
    parts = name.split("/")
    directories = {".git", ".ssh", ".aws", ".azure", ".gnupg", ".kube"}
    if any(part.casefold() in directories for part in parts):
        return True
    path = PurePosixPath(name)
    lowered = path.name.lower()
    suffix = path.suffix.lower()
    secret_files = {
        ".credentials.json", ".netrc", ".token", "auth.json",
        "credentials.json", "secrets.json", "token.json",
        "id_dsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_rsa",
    }
    credential_files = {
        ".netrc", "_netrc", ".git-credentials", ".pgpass",
        "id_dsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_rsa",
    }
    if (
        lowered in secret_files or lowered in credential_files
        or suffix in {".key", ".p12", ".pem", ".pfx"}
    ):
        return True
    if lowered in {".env", ".envrc"} or lowered.startswith(".env.") or suffix == ".env":
        return True
    return (
        suffix in {".cfg", ".ini", ".json", ".toml", ".txt", ".yaml", ".yml"}
        and ("credential" in lowered or "secret" in lowered)
    )


TRICKY_NAMES = [
    # unchanged base rules
    ".env", ".ENV", ".envrc", ".env.local", ".env.prod.kdbx", ".env.kdbx",
    "prod.env", "Secrets.ENV", "id_rsa", "ID_ED25519", "id_ed25519.pub",
    "key.pem", "KEY.PEM", "a.key.txt", "cert.p12", "cert.PFX", "x.pem.bak",
    ".netrc", "_NETRC", ".pgpass", ".git-credentials", "token.json", "AUTH.JSON",
    # *credential* / *secret* with config suffixes
    "application_default_credentials.json", "credentials.tfrc.json",
    "my-credentials.toml", "Credential.YAML", "secret.ini", "secrets.cfg",
    "notes-secret.txt", "credentials.md", "secret.py", "credentials.tfrc.md",
    # suffix stacking
    "vault.kdbx", "vault.KDBX", "vault.kdbx.bak", "archive.pem.kdbx", "db.kdbx.json",
    "credentials.kdbx", "secret.kdbx",
    # names added by this branch only
    ".npmrc", ".NPMRC", "pkg/.npmrc", "kubeconfig", ".htpasswd", ".boto", ".s3cfg",
    ".pypirc", ".terraformrc", "terraform.rc", ".vault-token", "service-account.json",
    "service_account.json", "serviceaccount.json", "service.json",
    # directories
    ".gcloud/access_tokens.db", ".config/gcloud/credentials.db", ".ssh/config",
    ".kube/config", ".azure/x", ".GIT/config", "src/.gcloud/x.txt",
    # ordinary
    "README.md", "src/app.py", "config.json", "npmrc.md", "hosts.yml",
]


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_extraction_refusals_match_the_pre_branch_predicate(name):
    refused = False
    try:
        archive_tools._portable_member_path(name, is_directory=False, max_depth=20)
    except archive_tools.ArchiveRejected as exc:
        refused = "sensitive" in str(exc)
    assert refused == _frozen_pre_branch_refuses(name), name


@pytest.mark.parametrize("name", TRICKY_NAMES)
def test_secret_predicate_is_base_or_added(name):
    from pathlib import Path
    path = Path(name)
    added = (
        path.name.lower() in file_ops.ADDED_CREDENTIAL_READ_FILES
        or path.suffix.lower() in file_ops.ADDED_SECRET_SUFFIXES
    )
    assert file_ops._is_base_secret_path(path) == _frozen_pre_branch_refuses(path.name)
    assert file_ops._is_secret_path(path) == (file_ops._is_base_secret_path(path) or added)


@pytest.mark.parametrize("name", [
    ".env", "id_rsa", ".netrc", ".pgpass", "key.pem",
    "application_default_credentials.json", "credentials.tfrc.json",
    ".env.kdbx", ".env.prod.kdbx", ".env.anything.kdbx",
])
def test_extraction_refuses_names_flagged_by_the_base_rules(workspace, name):
    _zip(workspace / "bad2.zip", [(name, b"SECRET")])
    data = archive_tools.list_archive("bad2.zip")
    assert data["valid"] is False
    assert "sensitive" in data["errors"][0]
