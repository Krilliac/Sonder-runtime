"""Every read-type tool the application executor runs honors the read guard.

``file_read_range`` already called ``file_ops.require_read_access`` before
the workbench primitive; ``image_inspect``, ``artifact_risk_inspect``,
``verify_artifact`` and ``archive_extract`` did not, so a credential store or
the personal corpus under an allowed root could be fingerprinted (sha256,
size, header, risk verdict) through them. The walks (``directory_tree``,
``file_find``, ``script_search``, ``secret_scan``) listed or scanned
credential-store entries; ``text_search`` already withheld them and keeps
doing so. A point read of a protected path is refused; a walk skips the
protected entries, as ``text_search`` does.
"""
from __future__ import annotations

import json
import zipfile

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall

MARK = "PLANTEDKEYMARK"
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAK%sFAKEFAKEFAKEFAKEFAKEFAKEFAKE\n"
    "-----END RSA PRIVATE KEY-----\n" % MARK
)
# A PNG header so the image path reports a format, dimensions and a hash.
PNG_KEY = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x10" * 2 + MARK.encode()


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
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "id_rsa").write_text(PRIVATE_KEY, encoding="utf-8")
    (ws / ".ssh" / "key.png").write_bytes(PNG_KEY)
    (ws / ".ssh" / "rc.sh").write_text("echo %s\n" % MARK, encoding="utf-8")
    (ws / "id_ed25519").write_text(PRIVATE_KEY, encoding="utf-8")
    (ws / ".env").write_text("API_KEY=%s-super-secret-value\n" % MARK, encoding="utf-8")
    corpus = ws / "sonder-personal-lora"
    corpus.mkdir()
    (corpus / "combined_personal.jsonl").write_text('{"chat": "%s"}\n' % MARK, encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / "ok.txt").write_text("%s ordinary\n" % MARK, encoding="utf-8")
    (ws / "src" / "tool.sh").write_text("echo ordinary\n", encoding="utf-8")
    (ws / "src" / "config.env").write_text("API_KEY=%s-ordinary-leak\n" % MARK, encoding="utf-8")
    return ws


def _run(tool, args):
    return ToolExecutorAdapter().execute(
        ToolCall(tool, args), local_owner_context(correlation_id="req_read_guard"),
    )


def _refused(result):
    assert result.ok is False, result.output
    assert result.error_code == "PermissionError", (result.error_code, result.output)
    assert "sha256" not in result.output
    assert MARK not in result.output


@pytest.mark.parametrize("path", [".ssh/key.png", ".ssh/id_rsa", "id_ed25519"])
def test_image_inspect_refuses_a_credential_store(workspace, path):
    _refused(_run("image_inspect", {"path": path}))


def test_image_inspect_refuses_the_personal_corpus_without_developer_auth(workspace):
    _refused(_run("image_inspect", {"path": "sonder-personal-lora/combined_personal.jsonl"}))


def test_image_inspect_still_inspects_an_ordinary_image(workspace):
    (workspace / "src" / "pic.png").write_bytes(PNG_KEY)
    result = _run("image_inspect", {"path": "src/pic.png"})
    assert result.ok, result.output
    assert result.evidence["format"] == "PNG"


@pytest.mark.parametrize("path", [
    ".ssh/id_rsa", "id_ed25519", "sonder-personal-lora/combined_personal.jsonl",
])
def test_artifact_risk_inspect_refuses_protected_paths(workspace, path):
    _refused(_run("artifact_risk_inspect", {"path": path}))


@pytest.mark.parametrize("path", [".ssh/id_rsa", "id_ed25519", ".env"])
def test_verify_artifact_refuses_a_credential_store(workspace, path):
    _refused(_run("verify_artifact", {"path": path}))


def test_archive_extract_refuses_a_protected_source(workspace):
    source = workspace / "sonder-personal-lora" / "bundle.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("chat.txt", MARK)
    result = _run("archive_extract", {
        "source": "sonder-personal-lora/bundle.zip", "destination": "out",
    })
    _refused(result)
    assert not (workspace / "out").exists()


@pytest.mark.parametrize("root", [".", ".ssh"])
def test_secret_scan_skips_credential_stores(workspace, root):
    result = _run("secret_scan", {"root": root})
    assert result.ok, result.output
    files = {finding["file"].replace("\\", "/") for finding in result.evidence["findings"]}
    assert not files & {"id_rsa", ".ssh/id_rsa", "id_ed25519", ".env"}, files
    assert result.evidence["credential_files_skipped"] >= 1
    if root == ".":
        # Ordinary workspace files are still scanned and reported, redacted.
        assert "src/config.env" in files
        assert "ordinary-leak" not in result.output


@pytest.mark.parametrize("path", [".", ".ssh"])
def test_directory_tree_skips_credential_stores(workspace, path):
    result = _run("directory_tree", {"path": path, "depth": 4, "include_hidden": True})
    assert result.ok, result.output
    listed = {row["relative"].replace("\\", "/") for row in result.evidence["entries"]}
    assert not any(
        name.startswith(".ssh") or name in {"id_rsa", "key.png", "rc.sh", "id_ed25519", ".env"}
        for name in listed
    ), listed
    assert result.evidence["skipped"] >= 1
    if path == ".":
        assert {"src", "src/ok.txt"} <= listed


@pytest.mark.parametrize("query", ["id_rsa", "id_ed25519", "key.png", ".env", ".ssh"])
def test_file_find_skips_credential_stores(workspace, query):
    result = _run("file_find", {"query": query})
    assert result.ok, result.output
    found = {row["relative"].replace("\\", "/") for row in result.evidence["results"]}
    assert not any(
        name.startswith(".ssh") or name in {"id_ed25519", ".env"} for name in found
    ), found


def test_file_find_still_finds_ordinary_files(workspace):
    result = _run("file_find", {"query": "ok.txt"})
    assert result.ok, result.output
    assert any(row["relative"].replace("\\", "/") == "src/ok.txt" for row in result.evidence["results"])


@pytest.mark.parametrize("root", [".", ".ssh"])
def test_script_search_skips_credential_stores(workspace, root):
    result = _run("script_search", {"root": root, "include_hidden": True})
    assert result.ok, result.output
    found = {row["relative"].replace("\\", "/") for row in result.evidence["results"]}
    assert "rc.sh" not in found and ".ssh/rc.sh" not in found, found
    if root == ".":
        assert "src/tool.sh" in found


@pytest.mark.parametrize("root", [".", ".ssh"])
def test_text_search_withholds_credential_stores(workspace, root):
    result = _run("text_search", {"query": MARK, "root": root, "include_hidden": True})
    assert result.ok, result.output
    matched = {row["relative"].replace("\\", "/") for row in result.evidence["matches"]}
    assert not any(name.startswith(".ssh") or name in {"rc.sh", "id_rsa", "id_ed25519", ".env"}
                   for name in matched), matched
    assert "super-secret" not in json.dumps(result.evidence)


def test_a_root_that_names_the_store_unlocks_it_as_file_read_does(workspace):
    (workspace.parent / "home" / "file_roots.local").write_text(
        str(workspace / ".ssh") + "\n", encoding="utf-8",
    )
    image = _run("image_inspect", {"path": str(workspace / ".ssh" / "key.png")})
    assert image.ok, image.output
    tree = _run("directory_tree", {"path": str(workspace / ".ssh")})
    assert tree.ok, tree.output
    assert {row["name"] for row in tree.evidence["entries"]} >= {"id_rsa", "key.png"}
