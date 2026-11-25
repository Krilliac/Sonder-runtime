"""Unit, security, persistence, and surface tests for Sonder's persistent scratchpad."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters import command_catalog
from sonder_runtime.adapters.persistence.scratchpad_store import (
    ScratchpadError,
    ScratchpadStore,
)
from sonder_runtime.application.server_core import agent_prompts, control_commands
from sonder_runtime.bootstrap.scratchpad import register_tools
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory.scratchpad import (
    ScratchpadPolicy,
    normalize_owner,
    normalize_tags,
    validate_note_name,
    validate_positive_int,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Domain validation


def test_validate_note_name_strips_md_and_rejects_unsafe_names():
    assert validate_note_name("plan") == "plan"
    assert validate_note_name("plan.md") == "plan"
    assert validate_note_name("Wave4_notes-2026.1") == "Wave4_notes-2026.1"

    for bad in ("", "   ", "../escape", "a/b", "a\\b", ".hidden", "_index", "CON", "nul.md", "a\x00b", "x" * 65):
        with pytest.raises(ValueError):
            validate_note_name(bad)


def test_normalize_owner_and_tags_and_bool_rejection():
    assert normalize_owner(None) == "local"
    assert normalize_owner("local") == "local"
    assert normalize_owner("alice") == "alice"
    assert normalize_owner("user@example.com") != normalize_owner("user+example.com")

    assert normalize_tags(["Plan", "plan", " wave-4 "]) == ("plan", "wave-4")
    with pytest.raises(TypeError):
        normalize_tags("not-a-list")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        normalize_tags(["bad tag!"])

    with pytest.raises(TypeError):
        validate_positive_int("max_lines", True, maximum=400)
    with pytest.raises(ValueError):
        validate_positive_int("start_line", 0, maximum=400)


# ---------------------------------------------------------------------------
# Store persistence, quotas, redaction, owner isolation, and reconciliation


def test_store_crud_persistence_append_search_and_reconcile(tmp_path):
    store = ScratchpadStore(tmp_path)

    w1 = store.write("alice", "sprint-plan", "# Plan\nStep 1: build\nStep 2: test", tags=["wave4", "plan"])
    assert w1["ok"] is True
    assert w1["created"] is True
    assert w1["note"]["tags"] == ["wave4", "plan"]

    # Append preserves existing text & tags
    w2 = store.write("alice", "sprint-plan.md", "Step 3: deploy", mode="append")
    assert w2["created"] is False
    assert w2["note"]["tags"] == ["wave4", "plan"]

    # Simulate restart with a fresh ScratchpadStore instance
    restarted = ScratchpadStore(tmp_path)
    r = restarted.read("alice", "sprint-plan", start_line=2, max_lines=2)
    assert r["ok"] is True
    assert r["start_line"] == 2
    assert r["end_line"] == 3
    assert r["total_lines"] == 4
    assert r["truncated"] is True
    assert r["content"] == "Step 1: build\nStep 2: test"
    assert r["framing"] == "scratchpad_working_notes_reference_only"

    # Search finds matching line across notes
    hits = restarted.search("alice", "DEPLOY")
    assert hits["count"] == 1
    assert hits["matches"][0]["name"] == "sprint-plan"
    assert hits["matches"][0]["line"] == 4

    # Human drops a Markdown file directly into alice's scratchpad directory
    manual = restarted.owner_dir("alice") / "human-note.md"
    manual.write_text("# Hand-edited note\nremember the milk\n", encoding="utf-8")
    listed = restarted.list_notes("alice")
    names = {n["name"] for n in listed["notes"]}
    assert names == {"sprint-plan", "human-note"}

    # Tag filter narrows list
    tagged = restarted.list_notes("alice", tag="wave4")
    assert [n["name"] for n in tagged["notes"]] == ["sprint-plan"]

    # Owner isolation: bob sees none of alice's notes
    assert restarted.list_notes("bob")["count"] == 0
    assert restarted.read("bob", "sprint-plan")["ok"] is False

    # Delete removes both file and index entry
    d1 = restarted.delete("alice", "sprint-plan")
    assert d1["deleted"] is True
    assert restarted.read("alice", "sprint-plan")["ok"] is False
    assert restarted.delete("alice", "sprint-plan")["deleted"] is False


def test_store_redacts_secrets_and_enforces_quotas(tmp_path):
    policy = ScratchpadPolicy(max_note_bytes=200, max_notes_per_owner=2, max_total_bytes_per_owner=300)
    store = ScratchpadStore(tmp_path, policy=policy)

    # Secrets are redacted before persisting to disk
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    store.write("local", "secrets-check", f"token={secret}")
    on_disk = (store.owner_dir("local") / "secrets-check.md").read_text(encoding="utf-8")
    assert secret not in on_disk

    # Per-note byte cap enforced fail-closed
    with pytest.raises(ScratchpadError, match="limit"):
        store.write("local", "too-big", "x" * 201)

    # Note count cap enforced fail-closed
    store.write("local", "note2", "y" * 100)
    with pytest.raises(ScratchpadError, match="note limit"):
        store.write("local", "note3", "z")

    # Owner total quota enforced on overwrite
    with pytest.raises(ScratchpadError, match="quota"):
        store.write("local", "secrets-check", "a" * 200)
        store.write("local", "note2", "b" * 150)


def test_store_refuses_symlinks(tmp_path):
    store = ScratchpadStore(tmp_path)
    odir = store.owner_dir("local")
    odir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch", encoding="utf-8")
    link = odir / "linked.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks unavailable without elevated privileges on this host")
    with pytest.raises(ScratchpadError, match="links or reparse"):
        store.read("local", "linked")
    with pytest.raises(ScratchpadError, match="links or reparse"):
        store.write("local", "linked", "overwrite")


# ---------------------------------------------------------------------------
# MCP registration, control commands, and permission invariants


def test_mcp_register_tools_and_invalid_input(tmp_path):
    class FakeMcp:
        def __init__(self) -> None:
            self.tools: dict[str, object] = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn

            return decorate

    fake = FakeMcp()
    cfg = SimpleNamespace(state=SimpleNamespace(home=str(tmp_path)), logging=SimpleNamespace(redact_patterns=()))
    sp_read, sp_write, sp_delete = register_tools(fake, config=cfg, home=tmp_path)
    assert set(fake.tools) == {"scratchpad_read", "scratchpad_write", "scratchpad_delete"}

    w = json.loads(sp_write("draft", "hello world", tags=["work"]))
    assert w["ok"] is True
    r = json.loads(sp_read(name="draft"))
    assert r["content"] == "hello world"
    s = json.loads(sp_read(query="hello"))
    assert s["count"] == 1
    d = json.loads(sp_delete("draft"))
    assert d["deleted"] is True

    with pytest.raises(InvalidInput):
        sp_write("../bad", "nope")
    with pytest.raises(InvalidInput):
        sp_read(name="draft", start_line=True)  # type: ignore[arg-type]


def test_control_command_parsing_and_permission_boundaries():
    assert control_commands.parse_control_kwargs("/scratchpad", "") == ({"_action": "read"}, "")
    assert control_commands.parse_control_kwargs("/scratchpad", "list") == ({"_action": "read"}, "")
    assert control_commands.parse_control_kwargs("/scratchpad", "read plan") == ({"_action": "read", "name": "plan"}, "")
    assert control_commands.parse_control_kwargs("/scratchpad", "search bug") == ({"_action": "read", "query": "bug"}, "")
    assert control_commands.parse_control_kwargs("/scratchpad", "write plan | do it") == (
        {"_action": "write", "name": "plan", "content": "do it", "mode": "replace"}, ""
    )
    assert control_commands.parse_control_kwargs("/scratchpad", "append plan | more") == (
        {"_action": "write", "name": "plan", "content": "more", "mode": "append"}, ""
    )
    assert control_commands.parse_control_kwargs("/scratchpad", "delete plan") == ({"_action": "delete", "name": "plan"}, "")
    assert control_commands.parse_control_kwargs("/scratchpad", "plan") == ({"_action": "read", "name": "plan"}, "")
    _, usage = control_commands.parse_control_kwargs("/scratchpad", "write missing_pipe")
    assert "usage:" in usage

    # Security boundary: only scratchpad_read is read-only; write/delete stay gated.
    assert "scratchpad_read" in command_catalog._READ_ONLY
    assert "scratchpad_write" not in command_catalog._READ_ONLY
    assert "scratchpad_delete" not in command_catalog._READ_ONLY
    assert "scratchpad_read" in agent_prompts.REPOSITORY_READ_ONLY_TOOLS
    assert "scratchpad_write" not in agent_prompts.REPOSITORY_READ_ONLY_TOOLS
    assert "scratchpad_delete" not in agent_prompts.REPOSITORY_READ_ONLY_TOOLS
