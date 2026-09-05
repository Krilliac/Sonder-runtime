"""Private recovery attempts own an exact selection and explicit host attachment."""

import pytest
from tests.test_app_managed_authority import managed, control


def test_recovery_attempt_rejects_missing_host_composition(managed):
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt

    authority, selection, *_ = managed
    with pytest.raises((TypeError, PermissionError)):
        AppWorkRecoveryAttempt(
            authority=authority,
            selection=selection,
            application=object(),
            recovery_factory=None,
            verifier_factory=None,
            approve_attachment=None,
            approve_verification=None,
            private_paths=None,
            model_writable_roots=None,
        )


def test_recovery_attempt_holds_selection_until_explicit_close(managed):
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt

    authority, selection, *_ = managed
    attempt = AppWorkRecoveryAttempt(
        authority=authority,
        selection=selection,
        application=object(),
        recovery_factory=lambda *args: None,
        verifier_factory=lambda *args: None,
        approve_attachment=lambda *args: None,
        approve_verification=lambda *args: None,
        private_paths=lambda: (),
        model_writable_roots=lambda: (),
    )
    with pytest.raises(PermissionError):
        authority.release_selection(selection)
    attempt.close()
    attempt.close()
    with pytest.raises(PermissionError):
        attempt.prepare(
            work_id="missing",
            attachment_command_id="attach",
            completion_command_id="complete",
        )
    assert not authority._selections


from tests.test_app_work_dispatcher import dispatch, prepare


def test_real_pending_work_explicitly_reattaches_and_certifies_once(
    dispatch, managed, monkeypatch, tmp_path
):
    from dataclasses import replace
    from types import SimpleNamespace
    import hashlib
    import json
    import time
    from sonder_runtime.bootstrap.app_recovery_coordinator import AppWorkRecoveryAttempt
    from sonder_runtime.bootstrap.managed_conversation import _ManagedTurn
    from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneRecovery
    from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from tests.test_continuation_approval_bridge import bridge
    from tests.test_delegated_verification import _verifier

    dispatcher, selection, models, lifetimes, _, fresh = dispatch
    authority, _, lanes, model, _, binding, *_ = managed
    ledger = ApprovalLedger(tmp_path / "explicit-recovery-approvals.db")
    gate = bridge(ledger)
    verified = []
    validation_observations = []
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
                command_id="recovery-child",
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
        validate = verifier.validate

        def observed_validate(*args, **kwargs):
            result = validate(*args, **kwargs)
            validation_observations.append(repr(result))
            return result

        verifier.validate = observed_validate
        require_current = verifier._require_current

        def observed_current(*args, **kwargs):
            try:
                return require_current(*args, **kwargs)
            except Exception as error:
                validation_observations.append(type(error).__name__ + ": " + str(error))
                raise

        verifier._require_current = observed_current
        capture = verifier.snapshotter.capture

        def observed_capture(*args, **kwargs):
            try:
                snapshot = capture(*args, **kwargs)
                validation_observations.append(
                    "manifest " + repr((snapshot.digest, snapshot.entries))
                )
                return snapshot
            except Exception as error:
                validation_observations.append(
                    "manifest " + type(error).__name__ + ": " + str(error)
                )
                raise

        verifier.snapshotter.capture = observed_capture
        execute = gateway.execute_check

        def checked(*args, **kwargs):
            execute(*args, **kwargs)
            for proof in proofs.values():
                proof["digest"] = hashlib.sha256(
                    json.dumps(
                        {k: v for k, v in proof.items() if k != "digest"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()

        gateway.execute_check = checked
        verified.append((verifier, gateway))
        view._session._approve = approve
        assert not view.verify_delegated(
            view._draft, verifier_factory=lambda *args: verifier
        ).valid
        original_stage(
            view, replace(facts, delegated_work=True, terminal_class="UNVERIFIED")
        )

    monkeypatch.setattr(_ManagedTurn, "stage_final", stage)
    dispatcher._eligibility = (
        lambda lifetime, turn, finalized: lifetime.terminal_eligibility(
            turn, verifier_factory=lambda *args: verified[0][0]
        )
    )
    work = prepare(dispatch)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    selected = fresh()
    original = dispatcher.status(selected, work_id=work.prepared.work_id)
    assert original.state == "verification_pending"
    application = lifetimes[0]._application

    private_paths = lambda: (binding.store.path,)
    model_roots = lambda: tuple(selected.context.workspace_roots)
    attachment_gate = [lambda *args: None]

    def recovery_factory(selected, record):
        host = authority.continuation_service(
            selected, projection_codec=TerminalProjectionCodec()
        )
        return ManagedStandaloneRecovery(
            controller=SimpleNamespace(run_id="explicit-recovery"),
            application=application,
            host=host,
            context=selected.context,
            host_conversation_id=selected.host_conversation_id,
            private_paths=private_paths,
            model_writable_roots=model_roots,
            approve_attachment=attachment_gate[0],
            approve_verification=approve,
        )

    attempt = AppWorkRecoveryAttempt(
        authority=authority,
        selection=selected,
        application=application,
        recovery_factory=recovery_factory,
        verifier_factory=lambda *args: verified[0][0],
        approve_attachment=approve,
        approve_verification=approve,
        private_paths=private_paths,
        model_writable_roots=model_roots,
    )
    try:
        with pytest.raises(PermissionError, match="exact private account recovery"):
            attempt.prepare(
                work_id=work.prepared.work_id,
                attachment_command_id="attach-original",
                completion_command_id="complete-original",
            )
        assert verified[0][1].calls == 0 and len(models) == 1
        attachment_gate[0] = approve
        prepared = attempt.prepare(
            work_id=work.prepared.work_id,
            attachment_command_id="attach-original",
            completion_command_id="complete-original",
        )
        assert attempt.inspect(prepared) == original
        with pytest.raises(PermissionError):
            attempt.resume(prepared)
        attachment = attempt.attach(prepared)
        assert attachment.phase == "attachment_pending"
        assert verified[0][1].calls == 0
        ledger.issue(
            attachment.approval.tool,
            attachment.approval.call_digest,
            approver="operator",
        )
        assert attempt.attach(prepared).phase == "attached"
        pending = attempt.resume(prepared)
        assert pending.phase == "approval_pending" and verified[0][1].calls == 0
        ledger.issue(
            pending.approval.tool, pending.approval.call_digest, approver="operator"
        )
        result = attempt.resume(prepared)
        if result.phase != "terminal":
            pytest.fail(
                result.phase
                + ": "
                + result.code
                + "\n"
                + "\n".join(validation_observations)
            )
        assert result.work.prepared == original.prepared
        assert result.work.terminal == original.verification_pending.original_terminal
        assert result.work.completion.phase == "certified_after_return"
        assert verified[0][1].calls == 1 and len(models) == 1

        def no_callbacks(*args, **kwargs):
            raise AssertionError("completed retry must be observational")

        monkeypatch.setattr(
            attempt._session, "resume_pending_verification", no_callbacks
        )
        monkeypatch.setattr(attempt._session, "terminal_eligibility", no_callbacks)
        assert attempt.resume(prepared) == result
        assert verified[0][1].calls == 1 and len(models) == 1
    finally:
        attempt.close()
