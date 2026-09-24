"""MEM-003: live managed work persists verifier learning through the real composition.

The work runs through the real ``AppManagedWorkDispatcher``, the bootstrap
``Application`` from ``server._application()``, a real
``ManagedStandaloneSession`` bound by ``AppManagedAuthority``, the real
delegated verifier, and the application-owned ``UnitOfWork``.  No eligibility
value is constructed by the test.
"""

from dataclasses import replace
import hashlib
import json
import time

import pytest

import server
from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.bootstrap.managed_learning import ManagedLearningRecorder
from tests.test_app_managed_authority import managed, control  # noqa: F401
from tests.test_app_work_dispatcher import dispatch, prepare  # noqa: F401


def _rows(db_path):
    with UnitOfWorkAdapter(db_path) as scope:
        return SQLiteVerifierObservationRepository(scope.connection).list_pairs(
            limit=100
        )


@pytest.mark.parametrize("check_exit_code", [0, 7], ids=["passed", "verified-failed"])
def test_live_managed_verifier_outcome_persists_authenticated_observation(
    dispatch, managed, monkeypatch, tmp_path, tmp_path_factory, check_exit_code
):
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.bootstrap.managed_conversation import _ManagedTurn
    from tests.test_continuation_approval_bridge import bridge
    from tests.test_delegated_verification import _verifier

    # Private stores must never overlap the model workspace under tmp_path.
    db_path = str(tmp_path_factory.mktemp("learning-private") / "memory.db")
    monkeypatch.setenv("SONDER_DB", db_path)
    dispatcher, selection, models, lifetimes, _, fresh = dispatch
    authority, _, lanes, model, context, binding, token, credential = managed
    gate = bridge(
        ApprovalLedger(tmp_path / "learning-approvals.db"),
        rule={"action": "allow", "pattern": "workspace_run"},
    )
    verified, certificates = [], []
    original_stage = _ManagedTurn.stage_final

    def approve(prepared, context):
        return gate.authorize(
            "workspace_run",
            prepared.approval_payload(),
            surface="app-control",
            expires_at=time.time() + min(120, context.remaining_seconds),
        )

    def stage(view, facts):
        with view._session._bound._scope() as current:
            child = lanes.spawn(
                command_id="learning-child",
                parent_session_id=view._session.parent_session_id,
                task="inspect",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
                max_wall_seconds=600,
            )["lane"]
        lanes.run_pending(child["id"], current)
        verifier, gateway, proofs = _verifier(
            (lanes, lanes.store, model, current.workspace_roots[0], current, {})
        )
        execute = gateway.execute_check

        def checked(*args, **kwargs):
            execute(*args, **kwargs)
            for proof in proofs.values():
                proof["status"] = "failed" if check_exit_code else "succeeded"
                proof["exit_code"] = check_exit_code
                proof["digest"] = hashlib.sha256(
                    json.dumps(
                        {k: v for k, v in proof.items() if k != "digest"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()

        gateway.execute_check = checked
        verified.append((verifier, gateway, child["id"]))
        view._session._approve = approve
        verdict = view.verify_delegated(
            view._draft, verifier_factory=lambda *args: verifier
        )
        assert verdict.valid is (check_exit_code == 0), verdict
        certificates.append(verdict)
        original_stage(
            view,
            replace(
                facts,
                delegated_work=True,
                validation_attempted=True,
                validation_passed=check_exit_code == 0,
                terminal_class="VALIDATION_FAILED" if check_exit_code else "NORMAL",
                certificate_id=verdict.certificate_id,
                certificate_generation=verdict.generation,
                certificate_code=verdict.code,
            ),
        )

    monkeypatch.setattr(_ManagedTurn, "stage_final", stage)
    from sonder_runtime.bootstrap import managed_terminal_eligibility as boundary

    resolutions = []
    real_boundary = boundary.terminal_eligibility

    def counted(*args, **kwargs):
        decision = real_boundary(*args, **kwargs)
        resolutions.append(decision)
        return decision

    monkeypatch.setattr(boundary, "terminal_eligibility", counted)
    verifier_factory = lambda *args: verified[0][0]  # noqa: E731
    dispatcher._eligibility = (
        lambda lifetime, turn, finalized: lifetime.terminal_eligibility(
            turn, verifier_factory=verifier_factory
        )
    )
    application = server._application()
    recorder = ManagedLearningRecorder(application, verifier_factory=verifier_factory)
    dispatcher._learning = recorder
    assert len(_rows(db_path)) == 0

    work = prepare(dispatch)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)

    # Observe with a newly bounded context so a slow host cannot turn the
    # fixture deadline into a spurious authentication failure.
    observer = authority.issue_selection(
        account_token=token,
        control_token=credential,
        context=replace(context, deadline_monotonic=time.monotonic() + 300),
    )
    try:
        record = dispatcher.status(observer, work_id=work.prepared.work_id)
    finally:
        authority.release_selection(observer)
    if check_exit_code:
        # A host-verified failure is negative learning evidence, never a
        # certificate that the outward work completed successfully.
        assert record.state == "unknown", record
        assert record.completion is None and record.terminal is None
    else:
        assert record.state == "terminal", record
        assert record.completion.phase == "certified"
    assert lifetimes[0]._application is application and len(models) == 1
    # The boundary (publication + manifest capture) is resolved exactly once.
    assert len(resolutions) == 1, resolutions

    outcomes = recorder.recent()
    assert [outcome.status for outcome in outcomes] == ["persisted"], (
        outcomes, [(decision.phase, decision.code) for decision in resolutions]
    )
    outcome = outcomes[0]
    # Replication is not configured in this composition; promotion must not
    # invent a fact source.
    assert outcome.promotion == "unconfigured"

    rows = _rows(db_path)
    assert len(rows) == 1
    receipt, observation = rows[0]
    assert observation.observation_id == outcome.observation_id
    assert receipt.verifier_outcome == ("failed" if check_exit_code else "passed")
    assert receipt.principal_id == selection.context.principal_id
    assert receipt.run_id == record.host_turn.run_id
    if not check_exit_code:
        assert receipt.receipt_id == record.terminal.receipt_digest
    assert observation.source == "authenticated_verifier"
    assert observation.trusted_source is True
    assert observation.positive is (check_exit_code == 0)
    assert observation.content.startswith("verified-subject:")
    # Independence is keyed by authenticated principal and scope (not lane).
    assert observation.independent_key == hashlib.sha256(
        json.dumps(
            {"principal_id": receipt.principal_id, "workspace_scope": receipt.project_scope},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    assert receipt.authorization is None  # never persisted with the row


def test_recorder_ignores_values_without_managed_verifier_authority(tmp_path):
    from tests.test_receipt_observation import _eligibility, _evidence

    recorder = ManagedLearningRecorder(object(), verifier_factory=lambda *a: None)
    forged = replace(_eligibility(_evidence(project="repo-a")), authority=None)
    assert recorder(object(), object(), forged).status == "not_applicable"
    assert recorder(object(), object(), object()).status == "not_applicable"
    # An issued authority on a non-managed owner is refused, not persisted.
    issued = _eligibility(_evidence(project="repo-a"))
    outcome = recorder(object(), object(), issued)
    assert outcome.status == "refused" and outcome.code == "MANAGED_OWNER_REQUIRED"
    assert [o.status for o in recorder.recent()] == [
        "not_applicable",
        "not_applicable",
        "refused",
    ]


def test_dispatcher_rejects_non_callable_learning():
    from sonder_runtime.bootstrap.app_managed_work import AppManagedWorkDispatcher

    with pytest.raises(TypeError):
        AppManagedWorkDispatcher(
            object(),
            object(),
            lifetime_factory=lambda *a: None,
            authorize_dispatch=lambda *a: None,
            terminal_eligibility=lambda *a: None,
            learning="not-callable",
        )
