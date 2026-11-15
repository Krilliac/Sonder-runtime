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
import subprocess
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
    """For secret and credential stores, search follows file_read's verdict.

    A planted secret or credential file is searchable exactly when an
    unauthorized file_read allows it. Pinned to ``file_read``'s own verdict,
    so credential names added to ``file_ops`` later are withheld from the
    search with no change here. Only secret and credential stores are
    covered: control-plane files that file_read also refuses without a
    developer token (``permissions.json``, ``system_profile.md``,
    ``workflows.json``, a root-level ``server.py``) stay searchable on
    purpose, because they are Sonder's own code and configuration, not
    credentials.
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


# --- Windows aliases: the walk's spelling must not differ from the file read.

windows_only = pytest.mark.skipif(
    os.name != "nt", reason="junctions and verbatim-path names are Windows-only",
)
VERBATIM_PREFIX = "\\\\?\\"  # the four characters \\?\


def _junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or not link.exists():
        pytest.skip("could not create a junction: %s" % completed.stderr.strip())


def _verbatim(path: Path) -> str:
    """Verbatim-path spelling, the only way to create a trailing-dot name."""
    return VERBATIM_PREFIX + str(path)


@windows_only
def test_junctions_into_credential_stores_are_not_followed(workspace, tmp_path):
    outside = tmp_path / "outside"
    (outside / ".docker").mkdir(parents=True)
    (outside / ".docker" / "config.json").write_text('{"auths": "%s-docker"}\n' % MARK)
    (outside / "sonder-personal-lora").mkdir()
    (outside / "sonder-personal-lora" / "adapter_config.json").write_text("%s-lora\n" % MARK)
    _junction(workspace / "jsub", workspace / ".aws")
    _junction(workspace / "dk", outside / ".docker")
    _junction(workspace / "lora", outside / "sonder-personal-lora")

    result = workbench.text_search(MARK, root=".", include_hidden=True)

    assert _matched(result) == {"src/ok.txt", "src/.hidden-notes"}
    assert result["skipped_by_reason"]["reparse_point"] == 3
    _assert_no_secret_content(repr(result))
    assert "-lora" not in repr(result)


@windows_only
def test_junction_out_of_every_root_is_not_followed(workspace, tmp_path):
    outside = tmp_path / "outside" / "plain"
    outside.mkdir(parents=True)
    (outside / "readme.txt").write_text("%s outside-roots\n" % MARK)
    (outside / "notes-outside.txt").write_text("x\n")
    _junction(workspace / "po", outside)

    search = workbench.text_search(MARK, root=".", include_hidden=True)
    mcp_search = server.text_search(MARK, include_hidden=True)
    found = file_ops.find_files("*", root=".", max_results=200)
    mcp_found = server.file_find("*")
    tree = workbench.directory_tree(".", depth=4, max_entries=200)
    inventory = workbench.workspace_inventory(".", top_n=50)

    assert _matched(search) == {"src/ok.txt", "src/.hidden-notes"}
    assert "outside-roots" not in mcp_search
    for listing in (search, mcp_search, found, mcp_found, tree, inventory):
        text = repr(listing)
        assert "readme.txt" not in text
        assert "notes-outside.txt" not in text


@windows_only
def test_trailing_dot_directory_beside_a_store_is_not_an_alias(workspace):
    alias = workspace / ".aws."
    os.mkdir(_verbatim(alias))
    try:
        result = workbench.text_search(MARK, root=".", include_hidden=True)
        listed = workbench.directory_tree(".", depth=4, include_hidden=True)
    finally:
        os.rmdir(_verbatim(alias))

    assert _matched(result) == {"src/ok.txt", "src/.hidden-notes"}
    assert result["skipped_by_reason"]["noncanonical_name"] == 1
    _assert_no_secret_content(repr(result))
    assert not any(row["name"] == ".aws." for row in listed["entries"])


@windows_only
@pytest.mark.parametrize("directory,name", [("keys", "id_rsa"), ("k", "server.pem")])
def test_trailing_dot_decoy_file_does_not_open_the_secret(workspace, directory, name):
    real = workspace / directory / name
    real.parent.mkdir()
    real.write_text("%s real-%s\n" % (MARK, name))
    decoy = _verbatim(real.parent / (name + "."))
    with open(decoy, "w", encoding="utf-8") as handle:
        handle.write("decoy\n")
    try:
        result = workbench.text_search(MARK, root=directory, include_hidden=True)
        output = server.text_search(MARK, root=directory)
    finally:
        os.remove(decoy)

    assert result["matches"] == []
    assert "real-" not in repr(result)
    assert "real-" not in output


@windows_only
def test_resolved_path_check_holds_when_the_walk_misses_an_alias(
    workspace, tmp_path, monkeypatch,
):
    """Second line of defence: judge and open the resolved file.

    The walk's alias checks are disabled here to stand in for an alias kind
    they do not know yet; the per-file check on the resolved path must still
    withhold the store behind a junction, the real key behind a trailing-dot
    decoy, and anything that resolves outside the search root.
    """
    monkeypatch.setattr(workbench, "_reparse_entry", lambda entry: entry.is_symlink())
    monkeypatch.setattr(workbench, "_noncanonical_name", lambda name: False)
    outside = tmp_path / "outside" / "plain"
    outside.mkdir(parents=True)
    (outside / "readme.txt").write_text("%s outside-roots\n" % MARK)
    _junction(workspace / "jsub", workspace / ".aws")
    _junction(workspace / "po", outside)
    (workspace / "keys").mkdir()
    (workspace / "keys" / "id_rsa").write_text("%s real-id_rsa\n" % MARK)
    decoy = _verbatim(workspace / "keys" / "id_rsa.")
    with open(decoy, "w", encoding="utf-8") as handle:
        handle.write("decoy\n")
    try:
        result = workbench.text_search(MARK, root=".", include_hidden=True)
    finally:
        os.remove(decoy)

    assert _matched(result) == {"src/ok.txt", "src/.hidden-notes"}
    assert result["skipped_by_reason"]["resolves_outside_root"] == 1
    _assert_no_secret_content(repr(result))
    assert "outside-roots" not in repr(result)
    assert "real-id_rsa" not in repr(result)
