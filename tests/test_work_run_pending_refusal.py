"""Tests for preserving and clearing ``pending_refusal`` in ``http_work_runs`` / ``/v1/work-runs``."""
from __future__ import annotations

import json
import time

import pytest

import permission_modes as pm
from sonder_runtime.adapters.persistence import http_work_runs
from sonder_runtime.bootstrap import chat_tools_wiring
from sonder_runtime.bootstrap.work_narration import enrich_work_record
from sonder_runtime.interfaces.http.facades.approvals import refusal_receipt
from sonder_runtime.interfaces.http.work_runs import _CURRENT_RUN, owner_scope
from tests.test_http_approvals import _http_server, _local_open, _request

pytestmark = pytest.mark.unit


def test_work_run_records_exposes_and_clears_pending_refusal(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(tmp_path / "work_runs.db"))
    monkeypatch.setattr(http_work_runs, "_INITIALIZED", set())
    run_id = "wr-" + ("a1" * 16)
    scope = owner_scope("local-open")
    http_work_runs.start(run_id, owner_scope=scope, process_id="p1", deadline_ts=time.time() + 600)

    decision = pm.Decision(
        pm.DENY, pm.MANUAL, "execution", "run_code needs approval", "run_code",
        source="unattended", call_id="1234abcd" * 2,
    )
    tok = _CURRENT_RUN.set(run_id)
    try:
        chat_tools_wiring.record_work_run_pending_refusal(
            run_id, decision, "run_code", {"code": "print('\u202ehi')"},
            refusal_receipt_fn=refusal_receipt,
            mode_label="manual", modes_allowing=["auto"],
        )
    finally:
        _CURRENT_RUN.reset(tok)

    row = http_work_runs.get(run_id, owner_scope=scope)
    assert row is not None
    assert row["pending_refusal"]["call_id"] == "1234abcd" * 2
    assert row["pending_refusal"]["bidi_warning"] is True
    assert "U+202E" in row["pending_refusal"]["bidi_chars"]

    enriched = enrich_work_record(row)
    assert enriched["pending_refusal"]["tool"] == "run_code"

    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "GET", f"/v1/work-runs/{run_id}")
        assert status == 200, payload
        data = json.loads(payload)
        assert data["pending_refusal"]["call_id"] == "1234abcd" * 2

    http_work_runs.finish(run_id, "returned", "done")
    finished = http_work_runs.get(run_id, owner_scope=scope)
    assert finished is not None
    assert finished["pending_refusal"] is None


def test_work_run_pending_refusal_strips_host_sentinels_and_credentials(monkeypatch, tmp_path):
    """Project-scoped dispatch injects an in-process ``object()`` approval sentinel.

    Unattended refusals (e.g. ``script_run`` / ``workspace_run`` / ``file_check``)
    during an HTTP work run or delegated child run must strip internal/credential
    keys before JSON serialization rather than raising ``TypeError``.
    """
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(tmp_path / "work_runs.db"))
    monkeypatch.setattr(http_work_runs, "_INITIALIZED", set())
    run_id = "wr-" + ("b2" * 16)
    scope = owner_scope("local-open")
    http_work_runs.start(run_id, owner_scope=scope, process_id="p1", deadline_ts=time.time() + 600)

    sentinel = object()
    for tool_name, args in (
        ("script_run", {"path": "gcd.py", "approval": sentinel, "extra_roots": str(tmp_path), "token": "secret-tok"}),
        ("workspace_run", {"program": "python", "args": ["gcd.py"], "approval": sentinel, "token": "secret-tok"}),
    ):
        decision = pm.Decision(
            pm.DENY, pm.MANUAL, "execution", f"{tool_name} needs approval", tool_name,
            source="unattended", call_id="abcd1234" * 2,
        )
        chat_tools_wiring.record_work_run_pending_refusal(
            run_id, decision, tool_name, args,
            refusal_receipt_fn=refusal_receipt,
            mode_label="manual", modes_allowing=["auto"],
        )
        row = http_work_runs.get(run_id, owner_scope=scope)
        assert row is not None
        assert row["pending_refusal"]["tool"] == tool_name
        assert "secret-tok" not in row["pending_refusal"]["detail"]
        assert "approval" not in row["pending_refusal"]["detail"]
        assert "extra_roots" not in row["pending_refusal"]["detail"]


def test_agent_dispatch_observed_refusal_in_work_run_does_not_raise_typeerror(monkeypatch, tmp_path):
    """End-to-end regression: project-scoped refusal inside an active work run returns policy text."""
    import server
    from sonder_runtime.adapters.security import permission_policy as pp

    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(tmp_path / "work_runs.db"))
    monkeypatch.setattr(http_work_runs, "_INITIALIZED", set())
    run_id = "wr-" + ("c3" * 16)
    scope = owner_scope("local-open")
    http_work_runs.start(run_id, owner_scope=scope, process_id="p1", deadline_ts=time.time() + 600)
    (tmp_path / "gcd.py").write_text("print(21)\n", encoding="utf-8")

    before_mode = pm.current_mode()
    pm.set_mode(pm.MANUAL)
    tok = _CURRENT_RUN.set(run_id)
    try:
        with pp.permission_policy.mode_scope("test-principal", "test-session"):
            out = server._agent_dispatch_observed(
                "script_run", {"path": "gcd.py"}, project=str(tmp_path), read_only=False,
            )
    finally:
        _CURRENT_RUN.reset(tok)
        pm.set_mode(before_mode)

    assert "HOST POLICY" in out
    assert "TypeError" not in out
    row = http_work_runs.get(run_id, owner_scope=scope)
    assert row is not None
    assert row["pending_refusal"] is not None
    assert row["pending_refusal"]["tool"] == "script_run"
