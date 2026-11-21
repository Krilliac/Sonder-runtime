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
import os
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


def _link(link, target, kind):
    """Create a junction or symlink, or skip where the OS will not allow one."""
    if kind == "junction":
        winapi = pytest.importorskip("_winapi", reason="junctions are Windows-only")
        if not hasattr(winapi, "CreateJunction"):
            pytest.skip("this Python cannot create a junction")
        try:
            winapi.CreateJunction(str(target), str(link))
        except OSError as exc:
            pytest.skip("cannot create a junction here: %s" % type(exc).__name__)
        return
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip("cannot create a symlink here: %s" % type(exc).__name__)


def _scanned_files(result):
    assert result.ok, result.output
    return {finding["file"].replace("\\", "/") for finding in result.evidence["findings"]}


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_secret_scan_does_not_follow_a_directory_link_out_of_the_roots(workspace, kind):
    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "leak.env").write_text("API_KEY=%s-outside-leak\n" % MARK, encoding="utf-8")
    _link(workspace / "src" / "out", outside, kind)
    result = _run("secret_scan", {"root": "."})
    files = _scanned_files(result)
    assert not any(name.startswith("src/out") for name in files), files
    assert result.evidence["links_skipped"] >= 1
    assert "skipped 1 symlink or junction" in result.output
    # The walk itself still works: an ordinary file beside the link is scanned.
    assert "src/config.env" in files


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_secret_scan_does_not_follow_a_directory_link_into_a_credential_store(workspace, kind):
    # Not a credential store by its own name: only its real location makes it one.
    (workspace / ".ssh" / "notes.txt").write_text(
        "API_KEY=%s-store-leak\n" % MARK, encoding="utf-8",
    )
    _link(workspace / "src" / "keys", workspace / ".ssh", kind)
    result = _run("secret_scan", {"root": "."})
    files = _scanned_files(result)
    assert not any(name.startswith("src/keys") for name in files), files
    assert result.evidence["links_skipped"] >= 1
    assert "src/config.env" in files


@pytest.mark.parametrize("target", ["store", "outside"])
def test_secret_scan_does_not_open_a_file_symlink(workspace, target):
    if target == "store":
        real = workspace / ".ssh" / "id_rsa"
    else:
        (workspace.parent / "outside").mkdir()
        real = workspace.parent / "outside" / "leak.env"
        real.write_text("API_KEY=%s-outside-leak\n" % MARK, encoding="utf-8")
    _link(workspace / "src" / "alias.txt", real, "symlink")
    result = _run("secret_scan", {"root": "."})
    files = _scanned_files(result)
    assert "src/alias.txt" not in files, files
    assert result.evidence["links_skipped"] >= 1
    assert "src/config.env" in files


def test_secret_scan_rechecks_the_resolved_path_of_every_file(workspace, monkeypatch):
    """A path whose real location is a credential store or outside the scan
    root is not opened, even when no link is visible on the walked path."""
    import sonder_runtime.adapters.secret_scan as secret_scan

    outside = workspace.parent / "outside"
    outside.mkdir()
    (outside / "leak.env").write_text("API_KEY=%s-outside-leak\n" % MARK, encoding="utf-8")
    (workspace / "src" / "moved.txt").write_text("API_KEY=%s-moved\n" % MARK, encoding="utf-8")
    (workspace / "src" / "stored.txt").write_text("API_KEY=%s-stored\n" % MARK, encoding="utf-8")
    real_resolve = secret_scan.Path.resolve

    def resolve(self, strict=False):
        if self.name == "moved.txt":
            return outside / "leak.env"
        if self.name == "stored.txt":
            return workspace / ".ssh" / "id_rsa"
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(secret_scan.Path, "resolve", resolve)
    result = _run("secret_scan", {"root": "."})
    files = _scanned_files(result)
    assert not files & {"src/moved.txt", "src/stored.txt"}, files
    assert result.evidence["links_skipped"] >= 1
    assert result.evidence["credential_files_skipped"] >= 1
    assert "src/config.env" in files


# ``harness_tools.secret_scan`` is the same tool through the other entry point
# (the ``secret_scan`` MCP tool and the agent dispatcher in server.py). It kept
# its own ``rglob`` walk, so it needs the same proofs as the adapter above.

def _harness_leak(path, tag):
    """A line the harness scanner's own patterns report (its API-key pattern
    does not match the adapter fixtures)."""
    path.write_text('password = "%s-%s"\n' % (MARK, tag), encoding="utf-8")


def _harness_scan(workspace, root=None):
    import harness_tools

    # The control every test below asserts on: an ordinary file is still found.
    _harness_leak(workspace / "src" / "plain.py", "ordinary-leak")
    result = harness_tools.secret_scan(root=str(root or workspace))
    assert result["ok"] is True, result
    return result, {finding["file"].replace("\\", "/") for finding in result["findings"]}


@pytest.mark.parametrize("root", [".", ".ssh"])
def test_harness_secret_scan_skips_credential_stores(workspace, root):
    result, files = _harness_scan(workspace, workspace / root)
    assert not files & {"id_rsa", ".ssh/id_rsa", "id_ed25519", ".env"}, files
    assert result["credential_files_skipped"] >= 1
    if root == ".":
        assert "src/plain.py" in files
        assert MARK not in json.dumps(result)


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_harness_secret_scan_does_not_follow_a_directory_link_out_of_the_roots(workspace, kind):
    outside = workspace.parent / "outside"
    outside.mkdir()
    _harness_leak(outside / "leak.env", "outside-leak")
    _link(workspace / "src" / "out", outside, kind)
    result, files = _harness_scan(workspace)
    assert not any(name.startswith("src/out") for name in files), files
    assert result["links_skipped"] >= 1
    assert "src/plain.py" in files


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_harness_secret_scan_does_not_follow_a_directory_link_into_a_credential_store(workspace, kind):
    _harness_leak(workspace / ".ssh" / "notes.txt", "store-leak")
    _link(workspace / "src" / "keys", workspace / ".ssh", kind)
    result, files = _harness_scan(workspace)
    assert not any(name.startswith("src/keys") for name in files), files
    assert result["links_skipped"] >= 1
    assert "src/plain.py" in files


@pytest.mark.parametrize("kind", ["junction", "symlink"])
def test_harness_secret_scan_does_not_descend_a_link_loop(workspace, kind):
    deep = workspace / "deep" / "a"
    deep.mkdir(parents=True)
    _harness_leak(deep / "leak.txt", "loop-leak")
    _link(deep / "loop", workspace, kind)
    result, files = _harness_scan(workspace)
    assert not any("loop" in name for name in files), files
    assert result["truncated"] is False
    assert result["links_skipped"] >= 1
    assert "deep/a/leak.txt" in files


@pytest.mark.parametrize("target", ["store", "outside"])
def test_harness_secret_scan_does_not_open_a_file_symlink(workspace, target):
    if target == "store":
        real = workspace / ".ssh" / "id_rsa"
    else:
        (workspace.parent / "outside").mkdir()
        real = workspace.parent / "outside" / "leak.env"
        _harness_leak(real, "outside-leak")
    _link(workspace / "src" / "alias.txt", real, "symlink")
    result, files = _harness_scan(workspace)
    assert "src/alias.txt" not in files, files
    assert result["links_skipped"] >= 1
    assert "src/plain.py" in files


def test_harness_secret_scan_rechecks_the_resolved_path_of_every_file(workspace, monkeypatch):
    """No link is created, so this one runs on every OS and account."""
    import sonder_runtime.adapters.secret_scan as secret_scan

    outside = workspace.parent / "outside"
    outside.mkdir()
    _harness_leak(outside / "leak.env", "outside-leak")
    _harness_leak(workspace / "src" / "moved.txt", "moved")
    _harness_leak(workspace / "src" / "stored.txt", "stored")
    real_resolve = secret_scan.Path.resolve

    def resolve(self, strict=False):
        if self.name == "moved.txt":
            return outside / "leak.env"
        if self.name == "stored.txt":
            return workspace / ".ssh" / "id_rsa"
        return real_resolve(self, strict=strict)

    # Resolve the root first: the patch below must not change what it resolves to.
    root = workspace.resolve()
    monkeypatch.setattr(secret_scan.Path, "resolve", resolve)
    result, files = _harness_scan(workspace, root)
    assert not files & {"src/moved.txt", "src/stored.txt"}, files
    assert result["links_skipped"] >= 1
    assert result["credential_files_skipped"] >= 1
    assert "src/plain.py" in files


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
