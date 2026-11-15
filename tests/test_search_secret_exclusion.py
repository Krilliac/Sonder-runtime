"""text_search never returns the contents of secret or credential files.

``text_search(query, include_hidden=True)`` used to open every file under the
root, so a literal or a ``.`` regex returned the matching lines of
``.ssh/id_rsa``, ``.env``, ``.git-credentials``, ``*.pem`` and the like -- files
``file_read`` refuses outright. The search now withholds every file
``file_read`` would refuse for being a secret or credential store (the same
``file_ops`` predicates, so a new credential name added there applies here),
counts what it withheld, and never lets ``include_hidden`` or a bypass
override it. An operator root that *names* a credential store keeps the
access ``file_read`` gives it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import server
import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench

MARK = "PLANTEDMARK"

# Files file_read refuses on main for being a secret or credential store.
SECRET_PLANTS = {
    ".ssh/id_rsa": (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAAFAKE%sFAKEAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "-----END OPENSSH PRIVATE KEY-----\n" % MARK
    ),
    ".aws/credentials": "%s-aws\n" % MARK,
    ".docker/config.json": '{"auths": "%s-docker"}\n' % MARK,
    ".env": "%s_API=fake-env-value\n" % MARK,
    ".git-credentials": "%s-gitcred\n" % MARK,
    "id_ed25519": "%s top-level key\n" % MARK,
    "service-credentials.json": '{"k": "%s-json"}\n' % MARK,
    "server.pem": "%s-pem\n" % MARK,
    "nested/project/.env.production": "%s_PROD=1\n" % MARK,
}
ORDINARY_PLANTS = {
    "src/ok.txt": "%s ordinary\n" % MARK,
    "src/.hidden-notes": "%s hidden but not secret\n" % MARK,
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(home / "file_roots.local"))
    for relative, body in {**SECRET_PLANTS, **ORDINARY_PLANTS}.items():
        target = ws / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return ws


def _matched(result):
    return {Path(row["relative"]).as_posix() for row in result["matches"]}


def _assert_no_secret_content(text):
    for fragment in ("-aws", "-docker", "_API=", "-gitcred", "top-level key",
                     "-json", "-pem", "_PROD", "FAKE%sFAKE" % MARK):
        assert fragment not in text, fragment


@pytest.mark.parametrize("query,regex", [(MARK, False), (".", True)])
def test_mcp_text_search_withholds_secret_contents_even_with_include_hidden(
    workspace, query, regex,
):
    output = server.text_search(query, regex=regex, include_hidden=True)

    assert not output.startswith("ERROR"), output
    _assert_no_secret_content(output)
    assert "ordinary" in output
    assert "hidden but not secret" in output
    # Withheld files are counted, never silently dropped.
    assert "skipped 7 secret/credential files and 2 sensitive directories" in output


def test_text_search_counts_withheld_files_by_name_only(workspace):
    result = workbench.text_search(MARK, root=".", include_hidden=True)

    assert _matched(result) == {"src/ok.txt", "src/.hidden-notes"}
    assert result["secret_files_skipped"] == 7
    assert result["sensitive_directories_skipped"] == 2
    assert result["skipped_by_reason"]["secret_file"] == 7
    assert result["skipped_by_reason"]["sensitive_directory"] == 2
    withheld = {
        Path(row["path"]).relative_to(workspace).as_posix()
        for row in result["skipped_examples"]
        if row["reason"] in ("secret_file", "sensitive_directory")
    }
    assert withheld == {
        ".aws", ".ssh", ".docker/config.json", ".env", ".git-credentials",
        "id_ed25519", "service-credentials.json", "server.pem",
        "nested/project/.env.production",
    }
    _assert_no_secret_content(repr(result))


def test_default_search_withholds_non_hidden_secret_files(workspace):
    result = workbench.text_search(MARK, root=".")

    assert _matched(result) == {"src/ok.txt"}
    _assert_no_secret_content(repr(result))


@pytest.mark.parametrize("glob", ["*.pem", "*.env*", "*.json", "id_*", "*credentials*"])
def test_explicit_glob_does_not_reach_secret_files(workspace, glob):
    result = workbench.text_search(MARK, root=".", glob=glob, include_hidden=True)

    assert result["matches"] == []
    _assert_no_secret_content(repr(result))


@pytest.mark.parametrize("root", [".ssh", ".aws", ".docker"])
def test_search_rooted_inside_a_credential_store_is_withheld(workspace, root):
    output = server.text_search(MARK, root=root, regex=False)

    assert not output.startswith("ERROR"), output
    _assert_no_secret_content(output)
    assert "(no matches)" in output
    assert "skipped 1 secret/credential files" in output


def test_bypass_does_not_unlock_secret_files_in_a_search(workspace):
    result = workbench.text_search(
        ".", root=".", regex=True, include_hidden=True, bypass=True,
    )

    assert _matched(result) == {"src/ok.txt", "src/.hidden-notes"}
    _assert_no_secret_content(repr(result))


def test_search_mirrors_file_read_for_every_planted_file(workspace):
    """Content is searchable exactly when an unauthorized file_read allows it.

    Pinned to ``file_read``'s own verdict, so credential names added to
    ``file_ops`` later are withheld from the search with no change here.
    """
    result = workbench.text_search(".", root=".", regex=True, include_hidden=True)
    matched = _matched(result)
    for relative in {**SECRET_PLANTS, **ORDINARY_PLANTS}:
        try:
            file_ops.read_file(relative)
            readable = True
        except PermissionError:
            readable = False
        assert (relative in matched) == readable, relative


def test_operator_root_naming_a_store_keeps_file_read_access(
    workspace, monkeypatch,
):
    # An operator root that IS the store: file_read allows .aws/credentials,
    # so the search does too. id_rsa stays refused by file_read without a
    # developer token (it is a secret by name), so the search withholds it.
    monkeypatch.setenv(
        "SONDER_FILE_ROOTS",
        os.pathsep.join((str(workspace / ".aws"), str(workspace / ".ssh"))),
    )
    assert file_ops.read_file(".aws/credentials")["text"].startswith(MARK)
    with pytest.raises(PermissionError):
        file_ops.read_file(".ssh/id_rsa")

    aws = workbench.text_search(MARK, root=str(workspace / ".aws"))
    ssh = workbench.text_search(MARK, root=str(workspace / ".ssh"))
    from_workspace = workbench.text_search(MARK, root=".", include_hidden=True)

    assert _matched(aws) == {"credentials"}
    assert ssh["matches"] == [] and ssh["secret_files_skipped"] == 1
    # Walking from the workspace reaches the named .aws store too; the
    # unnamed .docker store and every secret file are still withheld.
    assert ".aws/credentials" in _matched(from_workspace)
    assert ".ssh/id_rsa" not in _matched(from_workspace)
    assert ".docker/config.json" not in _matched(from_workspace)


def test_git_metadata_directory_is_pruned_with_include_ignored(workspace):
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[remote] url = https://%s@x.invalid\n" % MARK)
    (git_dir / "packed-refs").write_text("%s-ref\n" % MARK)

    result = workbench.text_search(
        MARK, root=".", include_hidden=True, include_ignored=True,
    )

    assert not any(row["relative"].startswith(".git") for row in result["matches"])
    assert result["skipped_by_reason"]["sensitive_directory"] == 3
