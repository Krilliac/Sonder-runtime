"""A one-shot approval spent on a call that did not take effect is given back.

Observed 2026-09-28: an operator approved one ``runtime_policy_update``; the
retry passed the gate and spent the approval, then the tool refused the update
in validation and changed nothing. The operator had to approve a third time.

The rule: an approval spent on a call whose tool *failed* (a legacy
``ERROR:`` reply or a raised error; a native ``isError`` receipt) is restored,
unless the tool starts host programs -- a build or script that fails has still
run, so for ``execution`` tools a failure is an effective use. At most one
effective use survives: a restored approval keeps its original expiry, a
revoked or expired one stays dead, and a call that succeeded spends it for good.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.security import approval_ledger as ledger_module

pytestmark = pytest.mark.unit


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ledger_module.ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    pm.reset_unattended_for_tests()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


def _issue(ledger, tool, args, **kwargs):
    return ledger.issue(tool, pm.call_digest(tool, args), approver="console operator", **kwargs)


# -- the ledger --------------------------------------------------------------

def test_restore_reopens_a_spent_approval_without_extending_it(ledger):
    args = {"path": "a.txt", "content": "x"}
    issued = _issue(ledger, "file_write", args)
    spent = ledger.consume("file_write", issued.digest)
    assert spent is not None and spent.spent
    restored = ledger.restore(issued.nonce, issued.digest)
    assert restored is not None and restored.open()
    assert restored.expires_ts == issued.expires_ts
    # Restoring what is not spent is a no-op; the digest must match too.
    assert ledger.restore(issued.nonce, issued.digest) is None
    ledger.consume("file_write", issued.digest)
    assert ledger.restore(issued.nonce, "0" * 64) is None


def test_revoked_or_expired_approvals_stay_dead(ledger, monkeypatch):
    args = {"path": "a.txt", "content": "x"}
    revoked = _issue(ledger, "file_write", args)
    ledger.consume("file_write", revoked.digest)
    with ledger._write() as conn:
        conn.execute("UPDATE approvals SET revoked_ts=? WHERE nonce=?", (time.time(), revoked.nonce))
    assert ledger.restore(revoked.nonce, revoked.digest) is None

    expired = _issue(ledger, "file_edit", args)
    ledger.consume("file_edit", expired.digest)
    real = time.time
    monkeypatch.setattr(ledger_module.time, "time", lambda: real() + expired.expires_ts - expired.issued_ts + 1)
    assert ledger.restore(expired.nonce, expired.digest) is None


# -- the engine ----------------------------------------------------------------

def test_a_failed_call_gets_its_approval_back_and_a_retry_runs(ledger):
    args = {"local_models_json": '{"code": "x"}'}
    issued = _issue(ledger, "runtime_policy_update", args)
    with pm.approval_call_scope() as spent:
        first = pm.decide("runtime_policy_update", interactive=False, surface="mcp", arguments=args)
        assert first.allowed and first.source == "approval"
        assert spent.restore() == 1
    assert ledger.get(issued.nonce).open()
    with pm.approval_call_scope() as spent:
        retry = pm.decide("runtime_policy_update", interactive=False, surface="mcp", arguments=args)
        assert retry.allowed and retry.source == "approval"
    # The retry took effect (no restore): the next call is refused.
    assert not pm.decide("runtime_policy_update", interactive=False, surface="mcp", arguments=args).allowed


def test_a_spend_is_given_back_at_most_once_and_only_inside_its_scope(ledger):
    args = {"local_models_json": '{"code": "x"}'}
    issued = _issue(ledger, "runtime_policy_update", args)
    with pm.approval_call_scope() as spent:
        assert pm.decide("runtime_policy_update", interactive=False, surface="mcp", arguments=args).allowed
        assert spent.restore() == 1
        assert spent.restore() == 0
    # Spent outside any scope: nothing records it, nothing can give it back.
    assert pm.decide("runtime_policy_update", interactive=False, surface="mcp", arguments=args).allowed
    with pm.approval_call_scope() as other:
        assert other.restore() == 0
    assert ledger.get(issued.nonce).spent


def test_an_execution_tool_that_failed_has_still_run(ledger):
    args = {"root": ".", "command": "make"}
    assert pm.risk_of("build_run") == "execution"
    issued = _issue(ledger, "build_run", args)
    with pm.approval_call_scope() as spent:
        assert pm.decide("build_run", interactive=False, surface="mcp", arguments=args).allowed
        assert spent.restore() == 0
    assert ledger.get(issued.nonce).spent


# -- the legacy MCP surface, end to end --------------------------------------------

def _mcp(name, args):
    return asyncio.run(server.mcp.call_tool(name, args))


def test_mcp_a_refused_policy_update_leaves_the_approval_open(ledger):
    # Malformed JSON: the tool refuses in validation and changes nothing.
    args = {"local_models_json": "{not json"}
    issued = _issue(ledger, "runtime_policy_update", args)
    result = _mcp("runtime_policy_update", args)
    assert result.is_error
    assert ledger.get(issued.nonce).open(), "a call that changed nothing must not spend the approval"
    assert pm.approval_spent_for("runtime_policy_update", args) is False


@pytest.fixture
def outside(tmp_path, monkeypatch):
    target = tmp_path / "outside"
    target.mkdir()
    return target


def test_mcp_an_effective_call_spends_the_approval_for_good(ledger, outside):
    target = outside / "note.txt"
    args = {"path": str(target), "content": "hello", "extra_roots": str(outside)}
    issued = _issue(ledger, "file_write", args)
    result = _mcp("file_write", args)
    assert not result.is_error, result
    assert target.read_text(encoding="utf-8") == "hello"
    assert ledger.get(issued.nonce).spent


# -- review findings (2026-09-28) ---------------------------------------------

def test_a_tool_off_the_allowlist_is_never_restored(ledger):
    # git_merge can stop on a conflict after rewriting the worktree: a failure
    # is not proof of no effect, so only allowlisted tools give approvals back.
    args = {"root": ".", "branch": "feature"}
    assert "git_merge" not in pm.RESTORABLE_ON_FAILURE
    issued = _issue(ledger, "git_merge", args)
    with pm.approval_call_scope() as spent:
        assert pm.decide("git_merge", interactive=False, surface="mcp", arguments=args).allowed
        assert spent.restore() == 0
    assert ledger.get(issued.nonce).spent


def test_mcp_a_raised_error_keeps_the_approval_spent(ledger, monkeypatch):
    # A raise can follow the effect (a post-call audit), so it is not given back.
    args = {"local_models_json": '{"code": "x"}'}
    issued = _issue(ledger, "runtime_policy_update", args)

    def boom(*_a, **_k):
        raise RuntimeError("audit failed after the write")

    monkeypatch.setattr(server, "runtime_policy_update", boom)
    tool = server.mcp._tool_manager.get_tool("runtime_policy_update")
    monkeypatch.setattr(tool, "fn", boom)
    with pytest.raises(Exception):
        result = _mcp("runtime_policy_update", args)
        if getattr(result, "is_error", False):
            raise RuntimeError("reported as error")
    assert ledger.get(issued.nonce).spent
