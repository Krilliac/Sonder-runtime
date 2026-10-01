"""Focused owner-surface coverage for playbook tools and CLI commands."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from sonder_runtime.adapters.playbook_review import approve_pending, content_digest, record_pending, reconcile
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.adapters.playbook_store import PlaybookStore
from sonder_runtime.bootstrap.playbooks import get_store, note_context, register_tools
from sonder_runtime.domain.memory.playbooks import PlaybookPolicy
from sonder_runtime.interfaces.cli import playbooks as cli


def _config(**values):
    defaults = dict(
        approval="required", categories=("pitfall", "procedure", "environment"),
        max_entry_bytes=4096, max_topic_bytes=16384, max_index_bytes=4096,
        max_topics=8, max_topics_per_turn=2, max_context_bytes=2048,
        environment_stale_days=30, measurement_stale_days=90,
    )
    defaults.update(values)
    return SimpleNamespace(playbooks=SimpleNamespace(**defaults), state=SimpleNamespace(home=""))


def test_cli_all_actions_and_optional_show(tmp_path, capsys):
    config = _config(approval="required")
    cli.configure_store_factory(get_store, note_context, approve_pending)
    store = get_store(config=config, home=tmp_path)
    entry = store.note("builds", "procedure", "Build it", "Run the bounded build.", "cmake --build build")
    record_pending(entry)
    assert cli.execute("list", SimpleNamespace(json=True), config=config, home=tmp_path) == 0
    assert cli.execute("show", SimpleNamespace(topic="builds", entry_id=None, json=True), config=config, home=tmp_path) == 0
    assert cli.execute("edit", SimpleNamespace(topic="builds", entry_id=entry["id"], title=None, body="Run it twice.", evidence=None, category=None, supersedes=None, triggers=None, json=True), config=config, home=tmp_path) == 0
    assert cli.execute("approve", SimpleNamespace(topic="builds", entry_id=entry["id"], json=True), config=config, home=tmp_path) == 0
    assert cli.execute("reject", SimpleNamespace(topic="builds", entry_id=entry["id"], json=True), config=config, home=tmp_path) == 0
    assert cli.execute("rm", SimpleNamespace(topic="builds", entry_id=entry["id"], json=True), config=config, home=tmp_path) == 0
    assert capsys.readouterr().out


def test_mcp_registration_guidance_bounded_read_and_taint(tmp_path):
    class FakeMcp:
        def __init__(self): self.tools = {}
        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    fake = FakeMcp()
    register_tools(fake, config=_config(approval="owner_corrections_auto"), home=tmp_path)
    assert set(fake.tools) == {"playbook_note", "playbook_read"}
    entry = json.loads(fake.tools["playbook_note"](
        "shells", "pitfall", "PowerShell gotcha", "Use the native separator.", "2026-09-30",
        ))
    assert entry["status"] == "proposed"
    get_store(config=_config(approval="owner_corrections_auto"), home=tmp_path).review("shells", entry["id"], "approved")
    assert "reference data" in fake.tools["playbook_read"]("shells")
    assert len(fake.tools["playbook_read"]("shells").encode()) <= 4096

    with note_context(tainted=False, owner_correction=True, provenance={"surface": "owner"}):
        approved = json.loads(fake.tools["playbook_note"](
            "shells", "pitfall", "Owner correction", "Use the checked command.", "owner correction",
        ))
    assert approved["status"] == "approved"


def test_pending_digest_binds_entry_content(tmp_path):
    store = PlaybookStore(tmp_path, PlaybookPolicy())
    entry = store.note("tools", "procedure", "Drive tool", "Use the fixed flags.")
    digest = content_digest(entry)
    changed = dict(entry, body="Use different flags.")
    assert digest != content_digest(changed)


def test_config_policy_propagates(tmp_path):
    config = _config(max_context_bytes=512, max_entry_bytes=512)
    store = get_store(config=config, home=tmp_path)
    assert store.policy.context_limit == 512
    assert store.policy.entry_limit == 512


def test_repl_correction_and_reload_are_trusted_and_bounded(tmp_path):
    cli.configure_store_factory(
        lambda *, config=None, home=None: get_store(config=_config(approval="owner_corrections_auto"), home=home),
        note_context, approve_pending, lambda: "reloaded",
    )
    result = cli.run_repl_command(
        'correction shells pitfall "Owner command" --body "Use the verified shell."',
        home=tmp_path,
    )
    assert '"status": "approved"' in result
    assert cli.run_repl_command("reload", home=tmp_path) == "playbook index reloaded"


def test_real_approval_ledger_reconcile_approves_exact_entry(tmp_path):
    store = PlaybookStore(tmp_path, PlaybookPolicy())
    entry = store.note("builds", "procedure", "Exact procedure", "Use the pinned command.")
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    digest = content_digest(entry)
    ledger.record_pending("playbook_entry_approval", digest, surface="playbook")
    ledger.issue("playbook_entry_approval", digest, approver="owner", surface="playbook")
    assert reconcile(store, ledger) == 1
    assert store.show("builds", entry["id"], approved_only=False)["status"] == "approved"


def test_reconcile_refuses_content_changed_after_issue(tmp_path):
    store = PlaybookStore(tmp_path, PlaybookPolicy())
    entry = store.note("builds", "procedure", "Mutable procedure", "Use the first command.")
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    digest = content_digest(entry)
    ledger.issue("playbook_entry_approval", digest, approver="owner", surface="playbook")
    store.edit("builds", entry["id"], body="Use the changed command.")
    assert reconcile(store, ledger) == 0
    assert store.show("builds", entry["id"], approved_only=False)["status"] == "proposed"
    assert ledger.approvals()


def test_cli_missing_entry_and_standalone_json(tmp_path):
    cli.configure_store_factory(get_store, note_context, approve_pending)
    missing = SimpleNamespace(topic="none", entry_id="missing", json=True)
    assert cli.execute("show", missing, home=tmp_path) == 1
    env = dict(os.environ)
    env["SONDER_HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "sonder_runtime", "playbooks", "list", "--json"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == []
