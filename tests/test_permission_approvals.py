"""One-shot approvals: exactly one refused call, approved once, spent once.

The gate refuses the effect classes when nobody can answer a mode's ``ask``.
A caller that passes its arguments gets a call id (the digest of the call)
in the refusal, and the call is noted as pending. An operator approves that
one call at the console (``/approve <call id>``) or through
``permission_approve``; the next unchanged call from any surface spends the
approval and runs, and the one after is refused again. The ledger stores
digests and a content-free preview, never arguments; a preflight neither
spends an approval nor leaves a request behind; ``plan``, an explicit deny
rule and the durable-authority class are untouched by approvals.
"""
from __future__ import annotations

import json
import os
import time

import pytest

import permission_modes as pm
import reloadable_mcp
import server
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security import permission_receipts

pytestmark = pytest.mark.unit

CALL = {"path": "notes.txt", "content": "hello", "mode": "create"}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A private ledger, installed as the one the gate and the tools consult."""
    store = ledger_module.ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    pm.reset_unattended_for_tests()
    yield store
    pm.reset_unattended_for_tests()


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, event_code, *, summary, detail=None, severity="INFO",
             correlation_id=None, operation_id=None):
        self.events.append({"code": event_code, "detail": dict(detail or {}),
                            "severity": severity})


@pytest.fixture
def sink():
    token = permission_receipts.snapshot()
    collector = _Sink()
    permission_receipts.install(lambda: collector)
    yield collector
    permission_receipts.restore(token)


# --- the digest ------------------------------------------------------------------


def test_the_digest_ignores_credentials_and_argument_order():
    base = pm.call_digest("file_write", CALL)
    assert len(base) == 64
    reordered = pm.call_digest("file_write", dict(reversed(list(CALL.items()))))
    with_credentials = pm.call_digest("file_write", {
        **CALL, "token": "t", "approval": object(), "bypass": True,
        "developer_authorized": True,
    })
    assert base == reordered == with_credentials
    assert pm.call_digest("file_write", {**CALL, "content": "goodbye"}) != base
    assert pm.call_digest("file_edit", CALL) != base
    assert pm.call_id(base) == base[:16]


def test_nothing_to_digest_means_no_call_id():
    assert pm.call_digest("file_write", None) == ""
    assert pm.call_digest("", CALL) == ""
    assert pm.call_digest("file_write", "not a mapping") == ""
    assert pm.call_id("") == ""


def test_the_preview_names_the_call_without_reproducing_it():
    preview = pm.argument_preview({
        "path": "notes.txt", "content": "x" * 500, "token": "secret", "mode": "create",
        "count": 3, "nested": {"a": 1},
    })
    assert "path=notes.txt" in preview and "mode=create" in preview
    assert "content=<500 chars>" in preview
    assert "secret" not in preview and "token" not in preview
    assert "count=3" in preview and 'nested={"a": 1}' in preview
    assert len(pm.argument_preview({"k": "v" * 400})) <= 200
    assert pm.argument_preview(None) == ""


# --- the ledger ---------------------------------------------------------------------


def test_an_approval_is_spent_exactly_once(ledger):
    digest = pm.call_digest("file_write", CALL)
    issued = ledger.issue("file_write", digest, approver="test", ttl_seconds=120)
    assert issued.nonce.startswith("apv_") and issued.open()
    assert ledger.approvals() == [issued]

    spent = ledger.consume("file_write", digest, surface="mcp")
    assert spent is not None and spent.nonce == issued.nonce
    assert spent.spent and spent.consumed_surface == "mcp"
    assert ledger.consume("file_write", digest, surface="mcp") is None
    assert ledger.approvals() == []
    assert ledger.get(issued.nonce).state() == "spent"


def test_an_approval_is_for_one_tool_and_one_digest(ledger):
    digest = pm.call_digest("file_write", CALL)
    ledger.issue("file_write", digest, approver="test")
    assert ledger.consume("file_edit", digest) is None
    assert ledger.consume("file_write", pm.call_digest("file_write", {**CALL, "mode": "append"})) is None
    assert ledger.consume("file_write", digest) is not None


def test_an_expired_or_revoked_approval_is_not_spendable(ledger, monkeypatch):
    digest = pm.call_digest("file_write", CALL)
    short = ledger.issue("file_write", digest, approver="test", ttl_seconds=60)
    real_time = time.time
    # The clock stays forward for the rest of the test, so ``short`` stays expired.
    monkeypatch.setattr(ledger_module.time, "time", lambda: real_time() + 61)
    assert ledger.consume("file_write", digest) is None
    assert ledger.get(short.nonce).state() == "expired"

    again = ledger.issue("file_write", digest, approver="test")
    assert ledger.revoke(again.nonce).revoked
    assert ledger.revoke(again.nonce) is None
    assert ledger.consume("file_write", digest) is None
    assert ledger.get(again.nonce).state() == "revoked"


def test_ttl_is_bounded():
    assert ledger_module.clamp_ttl(60) == 60
    for bad in (59, 86_401, "soon"):
        with pytest.raises(ledger_module.ApprovalLedgerError):
            ledger_module.clamp_ttl(bad)


def test_pending_calls_count_up_and_resolve_by_prefix(ledger):
    digest = pm.call_digest("file_write", CALL)
    first = ledger.record_pending("file_write", digest, surface="mcp", preview="path=notes.txt")
    second = ledger.record_pending("file_write", digest, surface="agent", preview="")
    assert (first.count, second.count) == (1, 2)
    assert second.preview == "path=notes.txt"  # an empty later preview keeps the first
    assert ledger.pending() == [second]

    resolved = ledger.resolve_call(pm.call_id(digest))
    assert resolved.digest == digest
    with pytest.raises(ledger_module.ApprovalLedgerError):
        ledger.resolve_call("abc")  # too short
    with pytest.raises(ledger_module.ApprovalLedgerError):
        ledger.resolve_call("0" * 16 if not digest.startswith("0" * 16) else "f" * 16)

    other = pm.call_digest("file_write", {**CALL, "content": "other"})
    if other[:8] == digest[:8]:  # astronomically unlikely; keep the test honest
        pytest.skip("digest prefixes collided")
    ledger.record_pending("file_write", other, surface="mcp")
    assert ledger.resolve_call(other[:8]).digest == other


def test_previews_are_redacted_before_they_are_stored(tmp_path):
    store = ledger_module.ApprovalLedger(
        tmp_path / "ledger.db", redact=lambda text: text.replace("hunter2", "[redacted]"),
    )
    digest = pm.call_digest("file_write", CALL)
    pending = store.record_pending("file_write", digest, preview="path=hunter2.txt")
    assert pending.preview == "path=[redacted].txt"
    assert "hunter2" not in store.format_status()


def test_the_ledger_follows_the_configured_home(tmp_path, monkeypatch):
    from sonder_runtime.platform import paths as runtime_paths

    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    try:
        assert ledger_module.default_ledger().path == str(tmp_path / "home" / "approvals.db")
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


# --- the decider ----------------------------------------------------------------------


def test_an_unattended_refusal_names_the_call_and_notes_it_as_pending(ledger, sink):
    decision = pm.decide("file_write", interactive=False, surface="mcp", arguments=CALL)
    assert decision.action == pm.DENY and decision.source == "unattended"
    assert decision.call_id == pm.call_id(pm.call_digest("file_write", CALL))
    assert "/approve %s" % decision.call_id in decision.reason

    pending = ledger.pending()
    assert [item.call_id for item in pending] == [decision.call_id]
    assert pending[0].tool == "file_write" and pending[0].surface == "mcp"
    assert "content=<5 chars>" in pending[0].preview and "hello" not in pending[0].preview

    detail = sink.events[-1]["detail"]
    assert detail["call_id"] == decision.call_id
    assert "hello" not in json.dumps(detail) and "notes.txt" not in json.dumps(detail)


def test_an_approval_answers_the_next_unchanged_call_once(ledger):
    refused = pm.decide("file_write", interactive=False, surface="mcp", arguments=CALL)
    assert refused.action == pm.DENY
    issued = ledger.issue("file_write", pm.call_digest("file_write", CALL),
                          approver="console operator")

    allowed = pm.decide("file_write", interactive=False, surface="http", arguments=CALL)
    assert allowed.action == pm.ALLOW and allowed.source == "approval"
    assert issued.nonce in allowed.reason and "console operator" in allowed.reason
    assert allowed.call_id == refused.call_id
    assert ledger.get(issued.nonce).consumed_surface == "http"
    assert ledger.pending() == []

    again = pm.decide("file_write", interactive=False, surface="http", arguments=CALL)
    assert again.action == pm.DENY and again.source == "unattended"


def test_a_changed_call_does_not_spend_the_approval(ledger):
    ledger.issue("file_write", pm.call_digest("file_write", CALL), approver="test")
    changed = pm.decide("file_write", interactive=False, surface="mcp",
                        arguments={**CALL, "content": "something else"})
    assert changed.action == pm.DENY
    assert len(ledger.approvals()) == 1


def test_a_preflight_neither_spends_nor_records(ledger):
    ledger.issue("file_write", pm.call_digest("file_write", CALL), approver="test")
    preflight = pm.decide("file_write", interactive=False, surface="preflight",
                          arguments=CALL, record=False)
    assert preflight.action == pm.DENY and preflight.call_id
    assert len(ledger.approvals()) == 1 and ledger.pending() == []


def test_an_approval_never_touches_plan_a_deny_rule_or_durable_authority(ledger, monkeypatch):
    digest = pm.call_digest("file_write", CALL)
    ledger.issue("file_write", digest, approver="test")

    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    assert pm.decide("file_write", interactive=False, arguments=CALL).source == "mode"
    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)

    denied = pm.decide(
        "file_write", interactive=False, arguments=CALL,
        rule_lookup=lambda tool: {"pattern": tool, "action": pm.DENY, "note": "no"},
    )
    assert denied.source == "rule"
    assert len(ledger.approvals()) == 1, "nothing above spent the approval"

    grant = {"call_id": "", "tool": "file_write", "arguments_json": "{}"}
    ledger.issue("permission_approve", pm.call_digest("permission_approve", grant), approver="test")
    durable = pm.decide("permission_approve", interactive=False, arguments=grant)
    assert durable.action == pm.DENY and durable.source == "durable-authority"


def test_a_call_without_arguments_has_no_call_id_and_leaves_nothing_pending(ledger):
    decision = pm.decide("file_write", interactive=False, surface="mcp")
    assert decision.action == pm.DENY and decision.call_id == ""
    assert "/approve" not in decision.reason
    assert ledger.pending() == []


def test_a_broken_ledger_leaves_the_refusal_as_it_was(monkeypatch):
    class _Broken:
        def consume(self, *args, **kwargs):
            raise RuntimeError("ledger is down")

        def record_pending(self, *args, **kwargs):
            raise RuntimeError("ledger is down")

    monkeypatch.setattr(pm, "_approval_ledger", lambda: _Broken())
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    decision = pm.decide("file_write", mode=pm.MANUAL, interactive=False, arguments=CALL)
    assert decision.action == pm.DENY and decision.source == "unattended"
    assert decision.call_id


# --- the surfaces --------------------------------------------------------------------


def test_the_legacy_mcp_gate_carries_the_arguments(ledger):
    with pytest.raises(reloadable_mcp.ToolError) as caught:
        reloadable_mcp._refuse_if_gated("file_write", CALL)
    call = pm.call_id(pm.call_digest("file_write", CALL))
    assert "/approve %s" % call in str(caught.value)

    ledger.issue("file_write", pm.call_digest("file_write", CALL), approver="test")
    assert reloadable_mcp._refuse_if_gated("file_write", CALL) is None
    with pytest.raises(reloadable_mcp.ToolError):
        reloadable_mcp._refuse_if_gated("file_write", CALL)


def test_the_agent_gate_carries_the_arguments_and_names_the_remedy(ledger):
    refusal = server._agent_permission_gate_error("file_write", CALL)
    call = pm.call_id(pm.call_digest("file_write", CALL))
    assert refusal.startswith("ERROR: HOST POLICY: tool 'file_write' is refused")
    assert "pending call %s" % call in refusal and "/approve %s" % call in refusal

    ledger.issue("file_write", pm.call_digest("file_write", CALL), approver="test")
    assert server._agent_permission_gate_error("file_write", CALL) == ""
    assert server._agent_permission_gate_error("file_write", CALL).startswith("ERROR:")


def test_the_control_chain_carries_the_arguments_for_a_named_tool(ledger):
    refused = server.control_command("/file_write path=notes.txt content=hello mode=create")
    assert refused.startswith("refused /file_write")
    pending = ledger.pending()
    assert len(pending) == 1 and pending[0].tool == "file_write"
    assert pending[0].surface == "control"
    assert refused.split("/approve ")[1].startswith(pending[0].call_id)


# --- the operator's tools -------------------------------------------------------------


def test_approving_needs_authority(ledger, monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_PERMISSION_EDITS", raising=False)
    out = server.permission_approve(call_id="0123456789abcdef")
    assert out.startswith("ERROR: one-shot approvals need the console")
    assert ledger.approvals() == []


def test_the_console_approves_a_pending_call_by_id_and_can_revoke_it(ledger):
    refused = pm.decide("file_write", interactive=False, surface="mcp", arguments=CALL)
    call = refused.call_id

    listing = server.control_command("/approvals", operator_approved=True)
    assert call in listing and "file_write" in listing

    approved = server.control_command("/approve %s 120" % call, operator_approved=True)
    assert approved.startswith("approved file_write call %s once" % call), approved
    open_approvals = ledger.approvals()
    assert len(open_approvals) == 1 and open_approvals[0].approver == "console operator"
    assert open_approvals[0].surface == "console"
    assert open_approvals[0].expires_ts - open_approvals[0].issued_ts == pytest.approx(120, abs=2)

    nonce = open_approvals[0].nonce
    assert nonce in server.control_command("/approvals", operator_approved=True)
    revoked = server.control_command("/approve revoke %s" % nonce, operator_approved=True)
    assert revoked.startswith("revoked %s" % nonce)
    assert ledger.approvals() == []
    assert pm.decide("file_write", interactive=False, surface="mcp", arguments=CALL).action == pm.DENY


def test_the_console_can_approve_an_exact_call_before_it_is_made(ledger):
    spec = json.dumps({"path": "notes.txt", "content": "hello", "mode": "create"})
    out = server.control_command("/approve call file_write %s" % spec, operator_approved=True)
    assert out.startswith("approved file_write call")
    allowed = pm.decide("file_write", interactive=False, surface="mcp", arguments=CALL)
    assert allowed.action == pm.ALLOW and allowed.source == "approval"


def test_approve_usage_and_bad_input_are_explained(ledger):
    assert server.control_command("/approve", operator_approved=True).startswith("usage: /approve")
    assert server.control_command("/approve revoke", operator_approved=True).startswith("usage:")
    assert server.control_command("/approve abc", operator_approved=True).startswith(
        "ERROR: a call id is at least")
    assert server.control_command(
        "/approve call file_write not-json", operator_approved=True,
    ).startswith("ERROR: arguments_json must be a JSON object")
    assert server.control_command("/approve revoke apv_nothing", operator_approved=True).startswith(
        "ERROR: no open approval apv_nothing")


def test_nobody_but_an_attended_operator_reaches_approve_through_the_chain(ledger):
    """From chat or a protocol caller the chain gate refuses before the branch runs."""
    refused = server.control_command("/approve 0123456789abcdef")
    assert refused.startswith("refused /approve")
    assert "permission_approve" in refused
    assert ledger.approvals() == []
    # The read-only listing is a safe read and answers anybody.
    assert server.control_command("/approvals").startswith("one-shot approvals")


def test_the_env_opt_in_and_a_developer_token_can_approve_too(ledger, monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    out = server.permission_approve(tool="file_write", arguments_json=json.dumps(CALL))
    assert out.startswith("approved file_write call")
    assert ledger.approvals()[0].approver == "env:SONDER_ALLOW_PERMISSION_EDITS"
    assert ledger.approvals()[0].surface == "tool"


def test_the_approval_tools_are_operator_only_and_graded_dangerous():
    import command_catalog

    assert "permission_approve" in server._AGENT_SYSTEM_OPERATOR_TOOLS
    assert "permission_approve" in command_catalog._DANGEROUS
    assert "permission_approve" in pm.DURABLE_AUTHORITY_TOOLS
    assert pm.risk_of("permission_approve") == "dangerous"
    assert pm.risk_of("permission_approvals") == "safe"
    console = command_catalog.console_tools()
    assert console["/approve"] == ("permission_approve",)
    assert console["/approvals"] == ("permission_approvals",)


# --- the reach an approval carries ------------------------------------------------


@pytest.fixture
def outside(tmp_path, monkeypatch):
    """A workspace, and a directory outside every configured root."""
    root = tmp_path / "ws"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(server.file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    from sonder_runtime.platform import paths as runtime_paths
    from sonder_runtime.bootstrap import app as bootstrap_app

    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(server, "_APP_GRAPH", bootstrap_app.build_application())
    try:
        yield elsewhere
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def test_a_spent_approval_carries_exactly_the_reach_the_operator_approved(ledger, outside):
    target = outside / "note.txt"
    line = "/file_write path=%s content=hello extra_roots=%s" % (target, outside)

    refused = server.control_command(line)
    assert refused.startswith("refused /file_write")
    assert not target.exists()
    pending = ledger.pending()
    assert len(pending) == 1
    assert "extra_roots=" in pending[0].preview

    ledger.issue("file_write", pending[0].digest, approver="console operator")
    written = server.control_command(line)
    assert written.startswith("file write"), written
    assert target.read_text(encoding="utf-8") == "hello"
    assert pm.approval_spent_for("file_write", {}) is False, "the note is cleared after the call"

    again = server.control_command(line)
    assert again.startswith("refused /file_write")
    assert target.read_text(encoding="utf-8") == "hello"


def test_the_reach_is_the_approved_roots_and_containment_still_holds(ledger, outside, tmp_path):
    beyond = tmp_path / "beyond"
    beyond.mkdir()
    target = beyond / "note.txt"
    # Approved with extra_roots naming a different directory: the call still
    # cannot reach outside the roots it named.
    line = "/file_write path=%s content=hello extra_roots=%s" % (target, outside)
    server.control_command(line)
    ledger.issue("file_write", ledger.pending()[0].digest, approver="console operator")
    out = server.control_command(line)
    assert out.startswith("ERROR:") and "outside allowed roots" in out
    assert not target.exists()


def test_reach_never_leaks_past_the_call(ledger, outside):
    target = outside / "note.txt"
    line = "/file_write path=%s content=hello extra_roots=%s" % (target, outside)
    server.control_command(line)
    ledger.issue("file_write", ledger.pending()[0].digest, approver="console operator")
    assert server.control_command(line).startswith("file write")
    # Nothing installed a reach scope for a plain internal call afterwards.
    assert server.file_ops._CALL_REACH.get() == ()
    assert server.file_write(str(outside / "other.txt"), "x", extra_roots=str(outside)).startswith("ERROR:")


def test_a_model_never_holds_a_credential(monkeypatch, ledger):
    seen = {}

    def spy(tool_name, args=None, **kwargs):
        seen["args"] = dict(args or {})
        return "ERROR: HOST POLICY: stop here"

    monkeypatch.setattr(server, "_agent_permission_gate_error", spy)
    server._agent_dispatch("file_write", {"path": "a.txt", "content": "x", "token": "t",
                                          "approval": "code"})
    assert "token" not in seen["args"] and "approval" not in seen["args"]
    sentinel = server._TRUSTED_REPOSITORY_APPROVAL
    server._agent_dispatch("file_write", {"path": "a.txt", "content": "x", "approval": sentinel})
    assert seen["args"]["approval"] is sentinel, "the host's in-process sentinel is not a credential string"


def test_the_retired_code_warns_once_and_grants_nothing(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SONDER_FILE_APPROVAL_CODE", "let-me")
    server._RETIRED_APPROVAL_CODE_WARNED.clear()
    with caplog.at_level(logging.WARNING, logger="sonder.server"):
        assert server._file_bypass_allowed("", "let-me") is False
        assert server._file_bypass_allowed("", "let-me") is False
    warnings = [record for record in caplog.records if "no longer honoured" in record.getMessage()]
    assert len(warnings) == 1
    assert "let-me" not in warnings[0].getMessage()

