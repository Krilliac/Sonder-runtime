"""C1: secret / control-plane files must not be readable through the direct
guarded read tools without a developer token or bypass.

Before this hardening the sensitivity classifier was applied only on the WRITE
path (`_require_mutation_access`) and on the delegated-repository read lane
(`resolve_repository_read_path`). The first-party direct read tools
(`read_file`, `inspect_data`, and the workbench-backed `file_read_range` /
`image_inspect` server tools) enforced root containment only, so a local agent
turn -- including a prompt-injected one -- could read `.env`, `memory.db`, the
personal corpus, control-plane config, or a root-level Sonder module and relay
it out. These tests pin the closed behavior, the two escape hatches (developer
token / bypass), and prove neither the write guard nor the repository read lane
changed.
"""
from __future__ import annotations

import pytest

import file_ops
import server


# Every classified read family the guard must refuse. memory.db and the
# personal corpus are treated as first-class secrets.
CLASSIFIED_NAMES = [
    "memory.db",             # all stored memories
    "memory.db-wal",         # sqlite sidecar
    "combined_personal.jsonl",  # personal chat/training corpus
    ".env",                  # dotenv secret
    "secrets.json",          # secret bundle
    "credentials.json",      # secret bundle
    "server.pem",            # *.pem private material
    "permissions.json",      # control-plane config
    "workflows.json",        # control-plane config
]


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point every root helper at an isolated tmp workspace/home."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.sonder_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    return ws


@pytest.mark.parametrize("name", CLASSIFIED_NAMES)
def test_read_file_refuses_classified_path_for_non_developer(workspace, name):
    target = workspace / name
    target.write_text("TOP SECRET", encoding="utf-8")
    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.read_file(name)


def test_read_file_refuses_root_level_python_module(workspace):
    # Root-level Sonder modules are classified by the existing mutation
    # classifier; the read guard mirrors it.
    (workspace / "server.py").write_text("import os  # source", encoding="utf-8")
    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.read_file("server.py")


def test_read_file_allows_ordinary_workspace_file(workspace):
    # A nested project .py is not a root-level Sonder module and must stay
    # freely readable, exactly as before.
    (workspace / "project").mkdir()
    (workspace / "project" / "module.py").write_text("value = 1", encoding="utf-8")
    (workspace / "notes.txt").write_text("plain notes", encoding="utf-8")
    assert file_ops.read_file("notes.txt")["text"] == "plain notes"
    assert file_ops.read_file("project/module.py")["text"] == "value = 1"


def test_developer_token_can_read_a_secret(workspace):
    (workspace / ".env").write_text("API_KEY=abc", encoding="utf-8")
    out = file_ops.read_file(".env", developer_authorized=True)
    assert out["text"] == "API_KEY=abc"


def test_bypass_can_read_a_secret(workspace):
    (workspace / "secrets.json").write_text('{"k": "v"}', encoding="utf-8")
    out = file_ops.read_file("secrets.json", bypass=True)
    assert out["text"] == '{"k": "v"}'


def test_inspect_data_refuses_secret_for_non_developer(workspace):
    (workspace / "credentials.json").write_text('{"token": "secret"}', encoding="utf-8")
    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.inspect_data("credentials.json")


def test_inspect_data_refuses_memory_db_for_non_developer(workspace):
    (workspace / "memory.db").write_bytes(b"SQLite format 3\x00")
    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.inspect_data("memory.db")


def test_read_file_refuses_custom_named_fanout_receipt_store(workspace, monkeypatch):
    receipt = workspace / "runtime-receipts.sqlite"
    receipt.write_bytes(b"SQLite format 3\x00")
    monkeypatch.setenv("SONDER_FANOUT_DB", str(receipt))

    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.read_file(str(receipt))


def test_inspect_data_developer_can_read_secret(workspace):
    (workspace / "credentials.json").write_text('{"token": "s"}', encoding="utf-8")
    out = file_ops.inspect_data("credentials.json", developer_authorized=True)
    assert out["kind"] == "json"


def test_inspect_data_allows_ordinary_data_file(workspace):
    (workspace / "data.json").write_text('{"a": 1, "b": 2}', encoding="utf-8")
    out = file_ops.inspect_data("data.json")
    assert out["kind"] == "json" and out["key_count"] == 2


def test_require_read_access_helper_gates_for_workbench_tools(workspace):
    # The shared entry point used by file_read_range / image_inspect.
    (workspace / "id_rsa.pem").write_text("-----BEGIN-----", encoding="utf-8")
    with pytest.raises(PermissionError, match="protected Sonder secret/control-plane"):
        file_ops.require_read_access("id_rsa.pem")
    # Developer / bypass hatches both open it.
    assert file_ops.require_read_access("id_rsa.pem", developer_authorized=True)
    assert file_ops.require_read_access("id_rsa.pem", bypass=True)


def test_require_read_access_allows_ordinary_file(workspace):
    (workspace / "readme.md").write_text("hello", encoding="utf-8")
    assert file_ops.require_read_access("readme.md")


# --- server tool layer -----------------------------------------------------

def test_server_file_read_refuses_secret_without_token(workspace):
    (workspace / ".env").write_text("API_KEY=abc", encoding="utf-8")
    out = server.file_read(".env")
    assert out.startswith("ERROR:")
    assert "protected Sonder secret/control-plane" in out
    assert "API_KEY" not in out


def test_server_file_read_allows_secret_with_approval_bypass(workspace, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_APPROVAL_CODE", "let-me")
    (workspace / ".env").write_text("API_KEY=abc", encoding="utf-8")
    out = server.file_read(".env", approval="let-me")
    assert not out.startswith("ERROR:")
    assert "API_KEY=abc" in out


def test_server_data_inspect_refuses_memory_db_without_token(workspace):
    (workspace / "memory.db").write_bytes(b"SQLite format 3\x00")
    out = server.data_inspect("memory.db")
    assert out.startswith("ERROR:")
    assert "protected Sonder secret/control-plane" in out


def test_server_file_read_range_refuses_control_plane(workspace):
    (workspace / "permissions.json").write_text('{"a": 1}', encoding="utf-8")
    out = server.file_read_range("permissions.json")
    assert out.startswith("ERROR:")
    assert "protected Sonder secret/control-plane" in out


def test_server_image_inspect_refuses_secret(workspace):
    (workspace / "server.key").write_text("private", encoding="utf-8")
    out = server.image_inspect("server.key")
    assert out.startswith("ERROR:")
    assert "protected Sonder secret/control-plane" in out


def test_server_file_read_range_allows_ordinary_file(workspace):
    (workspace / "notes.txt").write_text("line one\nline two\n", encoding="utf-8")
    out = server.file_read_range("notes.txt", start_line=1, end_line=2)
    assert not out.startswith("ERROR:")
    assert "line one" in out


# --- proofs the write path and repository lane are unchanged ---------------

def test_write_guard_unchanged_still_refuses_control_plane(workspace):
    # The mutation guard is untouched: a control-plane write without a
    # developer token is still refused with its own (write-side) message.
    (workspace / "permissions.json").write_text("before", encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file("permissions.json", "after", mode="overwrite")


def test_write_guard_unchanged_personal_corpus_still_writable(workspace):
    # The personal corpus is read-sensitive but was NOT added to the mutation
    # classifier, so writing it stays permitted -- proving the write path was
    # not broadened or otherwise touched.
    out = file_ops.write_file("combined_personal.jsonl", '{"x": 1}\n')
    assert out["bytes"] > 0
    assert (workspace / "combined_personal.jsonl").exists()


def test_write_guard_unchanged_ordinary_write_succeeds(workspace):
    out = file_ops.write_file("notes/todo.txt", "buy milk")
    assert out["bytes"] == len("buy milk")


def test_repository_read_lane_untouched_rejects_secret(workspace):
    (workspace / "secrets.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="secret or control state"):
        file_ops.resolve_repository_read_path("secrets.json")


def test_repository_read_lane_untouched_allows_ordinary_file(workspace):
    (workspace / "docs").mkdir()
    (workspace / "docs" / "guide.md").write_text("hi", encoding="utf-8")
    resolved = file_ops.resolve_repository_read_path("docs/guide.md")
    assert resolved == (workspace / "docs" / "guide.md").resolve()
