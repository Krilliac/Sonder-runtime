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
