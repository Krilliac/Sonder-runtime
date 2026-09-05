from sonder_runtime.bootstrap.app_managed_work import (
    AppManagedWorkDispatcher,
    dispatch_approval_arguments,
    dispatch_approval_digest,
)


def test_dispatcher_requires_real_host_composition():
    try:
        AppManagedWorkDispatcher(
            None,
            None,
            lifetime_factory=None,
            authorize_dispatch=None,
            terminal_eligibility=None,
        )
    except (TypeError, PermissionError):
        return
    raise AssertionError("missing private callbacks must refuse")


def test_owned_dispatcher_application_identity_is_read_only():
    app = object()
    dispatcher = AppManagedWorkDispatcher(
        object(),
        object(),
        application=app,
        lifetime_factory=lambda *args: None,
        authorize_dispatch=lambda *args: None,
        terminal_eligibility=lambda *args: None,
    )
    try:
        assert dispatcher.application is app
        with pytest.raises(AttributeError):
            dispatcher.application = object()
    finally:
        dispatcher.close()


import threading
import time
import pytest
import server
from tests.test_app_managed_authority import managed, control
from tests.test_tier_escalation import _install_agent_fakes
from sonder_runtime.bootstrap.prepared_workbench import PreparedWorkbenchAdapter
from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession
from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
from sonder_runtime.application.ports.app_control import CommandConflict, OutcomeUnknown
from sonder_runtime.application.ports.lane_continuation import GrantedApprovalEvidence


@pytest.fixture
def dispatch(managed, monkeypatch):
    authority, selection, lanes, model, context, binding, token, credential = managed
    models = _install_agent_fakes(
        monkeypatch, {"m-code": '{"final":"inspected repository"}'}
    )
    workbench = PreparedWorkbenchAdapter(
        server,
        policy_snapshot=lambda: dict(
            allowed_tools=["file_read"],
            allow_web=False,
            allow_location=False,
            revision=1,
        ),
    )
    app = server._application()
    lifetimes, approvals = [], []

    def lifetime_factory(selected):
        def require():
            authority.work_atomic(selected, selected.context, lambda tx: None)

        def session(controller, application):
            host = authority.continuation_service(
                selected, projection_codec=TerminalProjectionCodec()
            )
            return ManagedStandaloneSession(
                controller=controller,
                application=application,
                host=host,
                context=selected.context,
                host_conversation_id=selected.host_conversation_id,
                private_paths=lambda: (binding.store.path,),
                model_writable_roots=lambda: tuple(selected.context.workspace_roots),
                approve=lambda *args: approval(None, selected.context),
            )

        lifetime = ManagedConversationLifetime(
            application=app, session_factory=session, require_current=require
        )
        lifetimes.append(lifetime)
        return lifetime

    def approval(work, ctx):
        approvals.append(work)
        return GrantedApprovalEvidence(
            "workspace_run",
            dispatch_approval_digest(work) if work is not None else "a" * 64,
            "app-control",
            "test-policy-decision",
            "",
            (
                min(time.time() + 60, work.expires_at)
                if work is not None
                else time.time() + 60
            ),
            "policy",
        )

    def unavailable(*args):
        raise PermissionError("real terminal eligibility not composed in this fixture")

    dispatcher = AppManagedWorkDispatcher(
        authority,
        workbench,
        lifetime_factory=lifetime_factory,
        authorize_dispatch=approval,
        terminal_eligibility=unavailable,
    )

    def fresh():
        return authority.issue_selection(
            account_token=token, control_token=credential, context=context
        )

    yield dispatcher, selection, models, lifetimes, approvals, fresh
    dispatcher.close()


def prepare(dispatch):
    dispatcher, selection, *_ = dispatch
    return dispatcher.prepare(
        selection,
        command_id="work-command",
        request=dict(
            prompt="inspect repository", tier="code", allow_web=False, max_steps=1
        ),
    )


def test_owned_dispatcher_refuses_changed_application_before_run_binding(dispatch):
    base, selection, models, lifetimes, _, fresh = dispatch
    dispatcher = AppManagedWorkDispatcher(
        base.authority,
        base.workbench,
        application=object(),
        lifetime_factory=base._factory,
        authorize_dispatch=base._authorize,
        terminal_eligibility=base._eligibility,
    )
    try:
        work = prepare((dispatcher, selection))
        dispatcher.execute(selection, work_id=work.prepared.work_id)
        dispatcher._executor.shutdown(wait=True)
        observer = fresh()
        try:
            row = dispatcher.status(observer, work_id=work.prepared.work_id)
            assert row.state == "unknown" and row.run_id == ""
            assert row.host_turn is None and not models
        finally:
            dispatcher.authority.release_selection(observer)
    finally:
        dispatcher.close()


def test_preparation_is_immutable_retry_without_provider_or_expiry_renewal(dispatch):
    dispatcher, selection, models, *_ = dispatch
    first = prepare(dispatch)
    assert prepare(dispatch) == first
    assert not models
    with pytest.raises(CommandConflict):
        dispatcher.prepare(
            selection,
            command_id="work-command",
            request=dict(prompt="different", tier="code", allow_web=False),
        )


def test_real_model_waits_for_running_host_link_and_no_eligibility_cannot_terminalize(
    dispatch, monkeypatch
):
    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    calls = []

    def generate(model, *args, **kwargs):
        def invoke(*args, **kwargs):
            row = dispatcher.status(selection, work_id=work.prepared.work_id)
            assert row.state == "running" and row.host_turn.run_id == row.run_id
            calls.append(model)
            return '{"final":"inspected repository"}'

        return invoke

    monkeypatch.setattr(server, "_make_generate", generate)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    observer = fresh()
    try:
        row = dispatcher.status(observer, work_id=work.prepared.work_id)
        assert calls == ["m-code"]
        assert row.state == "unknown" and row.host_turn is not None
        assert row.interruption.code == "FINAL_PUBLICATION_UNKNOWN"
        assert dispatcher.execute(observer, work_id=row.prepared.work_id) == row
        assert len(approvals) == 1 and len(lifetimes) == 1
    finally:
        dispatcher.authority.release_selection(observer)


def test_failed_host_link_cas_prevents_model_and_never_constructs_second_controller(
    dispatch, monkeypatch
):
    from sonder_runtime.adapters.persistence.app_control import AppControlTransaction

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)

    def fail(*args, **kwargs):
        raise OSError("private injected DB detail must not be public")

    monkeypatch.setattr(AppControlTransaction, "bind_work_host", fail)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    observer = fresh()
    try:
        row = dispatcher.status(observer, work_id=work.prepared.work_id)
        assert not models and row.state == "unknown"
        assert row.interruption.code == "HOST_LINK_OUTCOME_UNKNOWN"
        assert row.run_id and row.host_turn is None
        assert dispatcher.execute(observer, work_id=row.prepared.work_id) == row
        assert len(lifetimes) == 1
    finally:
        dispatcher.authority.release_selection(observer)


def test_uncertain_submit_retains_slot_until_actual_callback_exits(
    dispatch, monkeypatch
):
    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    queued = []

    def ambiguous(callback, entry):
        queued.append((callback, entry))
        raise OSError("submit delivery unknown")

    monkeypatch.setattr(dispatcher._executor, "submit", ambiguous)
    with pytest.raises(OutcomeUnknown):
        dispatcher.execute(selection, work_id=work.prepared.work_id)
    observer = fresh()
    try:
        row = dispatcher.status(observer, work_id=work.prepared.work_id)
        assert row.state == "unknown"
        assert not dispatcher._slots.acquire(blocking=False)
        with pytest.raises(PermissionError):
            dispatcher.retry_cleanup(observer, work_id=work.prepared.work_id)
        assert dispatcher.execute(observer, work_id=work.prepared.work_id) == row
        callback, entry = queued.pop()
        callback(
            entry
        )  # Simulate the already-queued callback arriving after the lost response.
        assert not models and not lifetimes and not dispatcher._runs
        assert dispatcher.retry_cleanup(observer, work_id=work.prepared.work_id)
    finally:
        dispatcher.authority.release_selection(observer)


def test_lost_admission_commit_response_never_submits(dispatch, monkeypatch):
    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    original = dispatcher.authority.work_atomic
    lost = []

    def after_commit(selected, context, callback):
        result = original(selected, context, callback)
        from sonder_runtime.application.ports.app_managed_work import WorkAdmission

        if type(result) is WorkAdmission and not lost:
            lost.append(result)
            raise OSError("committed response lost")
        return result

    monkeypatch.setattr(dispatcher.authority, "work_atomic", after_commit)
    row = dispatcher.execute(selection, work_id=work.prepared.work_id)
    assert row.state == "unknown" and not models and not lifetimes
    assert not dispatcher._runs
    assert dispatcher.execute(selection, work_id=work.prepared.work_id) == row
    assert len(approvals) == 1


def test_pending_dispatch_approval_keeps_prepared_and_no_executor(dispatch):
    from sonder_runtime.application.ports.lane_continuation import (
        PendingApprovalEvidence,
        VerificationApprovalPending,
    )

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    pending = PendingApprovalEvidence(
        "app_work_execute", "a" * 64, "app-control", "a" * 16, time.time() + 60
    )

    def ask(*args):
        raise VerificationApprovalPending(pending)

    dispatcher._authorize = ask
    with pytest.raises(VerificationApprovalPending) as caught:
        dispatcher.execute(selection, work_id=work.prepared.work_id)
    assert caught.value.evidence == pending
    assert (
        dispatcher.status(selection, work_id=work.prepared.work_id).state == "prepared"
    )
    assert not dispatcher._runs and not models and not lifetimes


def test_real_dispatch_ledger_ask_approve_once_then_observe_without_respend(
    dispatch, tmp_path
):
    from tests.test_continuation_approval_bridge import bridge
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.application.ports.lane_continuation import (
        VerificationApprovalPending,
    )

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    ledger = ApprovalLedger(tmp_path / "dispatch-approval.db")
    gate = bridge(ledger)

    def authorize(prepared, context):
        return gate.authorize(
            "workspace_run",
            dispatch_approval_arguments(prepared),
            surface="app-control",
            expires_at=prepared.expires_at,
        )

    dispatcher._authorize = authorize
    with pytest.raises(VerificationApprovalPending) as pending:
        dispatcher.execute(selection, work_id=work.prepared.work_id)
    assert pending.value.evidence.call_digest == dispatch_approval_digest(work.prepared)
    assert (
        not models
        and dispatcher.status(selection, work_id=work.prepared.work_id).state
        == "prepared"
    )
    issued = ledger.issue(
        "workspace_run",
        pending.value.evidence.call_digest,
        approver="test-operator",
        surface="repl",
    )
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    assert ledger.get(issued.nonce).spent
    observer = fresh()
    try:
        before = len(models)
        row = dispatcher.execute(observer, work_id=work.prepared.work_id)
        assert row.state == "unknown" and len(models) == before
        assert ledger.pending() == []
    finally:
        dispatcher.authority.release_selection(observer)


def test_foreign_approval_digest_cannot_admit(dispatch):
    from dataclasses import replace

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    authorize = dispatcher._authorize
    dispatcher._authorize = lambda *args: replace(
        authorize(*args), call_digest="b" * 64
    )
    with pytest.raises(PermissionError):
        dispatcher.execute(selection, work_id=work.prepared.work_id)
    assert (
        dispatcher.status(selection, work_id=work.prepared.work_id).state == "prepared"
    )
    assert not models and not dispatcher._runs


@pytest.mark.parametrize("lost_terminal_response", [False, True])
def test_actual_sealed_no_delegation_terminal_is_recorded_without_model_replay(
    dispatch, monkeypatch, lost_terminal_response
):
    from sonder_runtime.application.ports.app_managed_work import AppWorkRecord

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    observed = []

    def no_verifier(*args):
        pytest.fail("no-delegation terminal must not execute or compose a verifier")

    def eligible(lifetime, expected, finalized):
        result = lifetime.terminal_eligibility(expected, verifier_factory=no_verifier)
        assert result.evidence.result == finalized
        assert result.evidence.facts.delegated_work is False
        observed.append(result)
        return result

    dispatcher._eligibility = eligible
    original = dispatcher.authority.work_atomic
    lost = []

    def commit_then_lose(selected, context, callback):
        result = original(selected, context, callback)
        if (
            lost_terminal_response
            and type(result) is AppWorkRecord
            and result.state == "terminal"
            and not lost
        ):
            lost.append(result)
            raise OSError("lost terminal commit response")
        return result

    monkeypatch.setattr(dispatcher.authority, "work_atomic", commit_then_lose)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    observer = fresh()
    try:
        row = dispatcher.status(observer, work_id=work.prepared.work_id)
        assert row.state == "terminal", row
        assert len(observed) == 1 and observed[0].eligible is True
        assert row.terminal == observed[0].evidence.result.receipt
        assert row.completion.phase == "not_required"
        assert row.completion.pending_identity is None
        assert row.completion.publication_receipt is None
        count = len(models)
        assert dispatcher.execute(observer, work_id=work.prepared.work_id) == row
        assert len(models) == count and len(approvals) == 1
        assert bool(lost) is lost_terminal_response
    finally:
        dispatcher.authority.release_selection(observer)


def test_closed_lifetime_failure_retains_exact_handle_and_capacity_for_retry(
    dispatch, monkeypatch
):
    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    factory = dispatcher._factory
    closes = []

    def wrapped(selected):
        lifetime = factory(selected)
        original = lifetime.close

        def close():
            closes.append(lifetime)
            if len(closes) == 1:
                raise OSError("close response unavailable")
            original()

        monkeypatch.setattr(lifetime, "close", close)
        return lifetime

    dispatcher._factory = wrapped
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    assert len(closes) == 1 and dispatcher._runs
    assert not dispatcher._slots.acquire(blocking=False)
    observer = fresh()
    try:
        assert dispatcher.retry_cleanup(observer, work_id=work.prepared.work_id)
        assert closes == [lifetimes[0], lifetimes[0]]
        assert not dispatcher._runs
    finally:
        dispatcher.authority.release_selection(observer)


def test_close_waits_for_committed_admission_before_registry_publication(
    dispatch, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from sonder_runtime.application.ports.app_managed_work import WorkAdmission

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    committed, publish = threading.Event(), threading.Event()
    original = dispatcher.authority.work_atomic

    def paused(selected, context, callback):
        result = original(selected, context, callback)
        if type(result) is WorkAdmission and result.newly_admitted:
            committed.set()
            assert publish.wait(15)
        return result

    monkeypatch.setattr(dispatcher.authority, "work_atomic", paused)
    dispatcher._factory = lambda selected: (_ for _ in ()).throw(
        PermissionError("test refuses host creation")
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        execute = pool.submit(
            dispatcher.execute, selection, work_id=work.prepared.work_id
        )
        assert committed.wait(15)
        close = pool.submit(dispatcher.close)
        deadline = time.monotonic() + 5
        while not dispatcher._closed and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert dispatcher._closed and not close.done()
        publish.set()
        assert execute.result(timeout=30).state == "admitted"
        close.result(timeout=30)
    assert not models and not dispatcher._runs and dispatcher._submitting == 0


def test_concurrent_execute_reserves_only_one_submission(dispatch):
    from concurrent.futures import ThreadPoolExecutor
    from sonder_runtime.application.ports.app_control import CapacityExceeded

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    work = prepare(dispatch)
    dispatcher.authority.release_selection(selection)
    first, second = fresh(), fresh()
    barrier = threading.Barrier(2)
    authorize = dispatcher._authorize

    def both(*args):
        result = authorize(*args)
        barrier.wait(timeout=15)
        return result

    dispatcher._authorize = both
    dispatcher._factory = lambda selected: (_ for _ in ()).throw(
        PermissionError("test refuses host creation")
    )

    def execute(selected):
        try:
            return dispatcher.execute(selected, work_id=work.prepared.work_id)
        except CapacityExceeded:
            dispatcher.authority.release_selection(selected)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, (first, second)))
    dispatcher._executor.shutdown(wait=True)
    assert len([r for r in results if r is not None]) == 1
    assert not models and not lifetimes and not dispatcher._runs


def test_selection_revocation_before_queued_callback_prevents_host_and_model(
    dispatch, managed, monkeypatch
):
    from concurrent.futures import Future
    from tests.test_app_control_http import invoke

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    authority, _, lanes, model, context, binding, token, credential = managed
    work = prepare(dispatch)
    queued = []

    def queue(callback, entry):
        future = Future()
        queued.append((callback, entry, future))
        return future

    monkeypatch.setattr(dispatcher._executor, "submit", queue)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    invoke(
        binding,
        token,
        "clear_selection",
        dict(command_id="clear-before-callback", expected_epoch=selection.slot.epoch),
        credential,
    )
    callback, entry, future = queued.pop()
    assert future.set_running_or_notify_cancel()
    callback(entry)
    future.set_result(None)
    assert not models and not lifetimes and not dispatcher._runs
    row = binding.store.atomic(
        lambda tx: tx.read_work(
            principal_id=selection.control.principal_id,
            control_session_id=selection.control.control_session_id,
            work_id=work.prepared.work_id,
        )
    )
    assert (
        row.state == "admitted"
    )  # Revocation also refuses a new uncertainty mutation.


@pytest.mark.parametrize(
    ("phase", "lose_pending_response"),
    [("unknown", False), ("approval_pending", False), ("approval_pending", True)],
)
def test_actual_false_terminal_eligibility_is_durable_and_never_redispatched(
    dispatch, managed, monkeypatch, tmp_path, phase, lose_pending_response
):
    """Scripted model, real host evidence/verifier/approval ledger and app DB."""
    from dataclasses import replace
    from sonder_runtime.bootstrap.managed_conversation import _ManagedTurn
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from tests.test_continuation_approval_bridge import bridge
    from tests.test_delegated_verification import _verifier

    dispatcher, selection, models, lifetimes, approvals, fresh = dispatch
    lanes = managed[2]
    ledger = ApprovalLedger(tmp_path / "pending-dispatch-approvals.db")
    gate = bridge(ledger)
    original_stage = _ManagedTurn.stage_final
    observed, prepared_verifiers, failures = [], [], []

    def stage(view, facts):
        if phase == "unknown":
            # Historical evidence has no positive declaration of delegation.
            return original_stage(view, replace(facts, delegated_work=None))
        bound = view._session._bound
        with bound._scope() as current:
            child = lanes.spawn(
                command_id="pending-child",
                parent_session_id=view._session.parent_session_id,
                task="Inspect disposable repository",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
            )["lane"]
        lanes.run_pending(child["id"], current)
        assert lanes.store.read_lane(child["id"])["status"] == "completed"
        verifier, gateway, _ = _verifier(
            (lanes, lanes.store, managed[3], current.workspace_roots[0], current, {})
        )
        prepared_verifiers.append((verifier, gateway))
        view._session._approve = lambda prepared, context: gate.authorize(
            "workspace_run",
            prepared.approval_payload(),
            surface="app-control",
            expires_at=min(time.time() + 60, time.time() + context.remaining_seconds),
        )
        verdict = view.verify_delegated(
            view._draft, verifier_factory=lambda *args: verifier
        )
        assert not verdict.valid and gateway.calls == 0
        assert bound.pending_verification() is not None
        original_stage(
            view, replace(facts, delegated_work=True, terminal_class="UNVERIFIED")
        )

    def eligibility(lifetime, expected, finalized):
        def verifier_factory(*args):
            assert phase == "approval_pending"
            return prepared_verifiers[0][0]

        actual = lifetime.terminal_eligibility(
            expected, verifier_factory=verifier_factory
        )
        assert actual.evidence.result == finalized
        assert not actual.eligible and actual.phase == phase
        observed.append(actual)
        return actual

    def checked_stage(*args):
        try:
            return stage(*args)
        except BaseException as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(_ManagedTurn, "stage_final", checked_stage)
    dispatcher._eligibility = eligibility
    atomic = dispatcher.authority.work_atomic
    lost = []

    def ambiguous_pending(*args, **kwargs):
        result = atomic(*args, **kwargs)
        if (
            lose_pending_response
            and not lost
            and getattr(result, "state", None) == "verification_pending"
        ):
            lost.append(result)
            raise OSError("injected lost pending commit response")
        return result

    monkeypatch.setattr(dispatcher.authority, "work_atomic", ambiguous_pending)
    work = prepare(dispatch)
    dispatcher.execute(selection, work_id=work.prepared.work_id)
    dispatcher._executor.shutdown(wait=True)
    observer = fresh()
    try:
        row = dispatcher.status(observer, work_id=work.prepared.work_id)
        assert not failures, repr(failures)
        assert len(observed) == 1
        assert row.state == (
            "verification_pending"
            if phase == "approval_pending" and not lose_pending_response
            else "unknown"
        )
        assert len(models) == 1
        if phase == "approval_pending":
            retained = row.verification_pending
            assert retained.identity == observed[0].pending_identity
            assert retained.approval == observed[0].pending_approval
            assert retained.original_terminal == observed[0].evidence.result.receipt
            assert ledger.resolve_call(retained.approval.call_digest) is not None
            assert prepared_verifiers[0][1].calls == 0
            if lose_pending_response:
                assert len(lost) == 1
                assert row.verification_pending == lost[0].verification_pending
                assert row.interruption.prior_state == "verification_pending"
                assert row.interruption.code == "FINAL_PUBLICATION_UNKNOWN"
        else:
            assert row.interruption.code == "FINAL_PUBLICATION_UNKNOWN"
        assert dispatcher.execute(observer, work_id=work.prepared.work_id) == row
        assert len(models) == 1 and len(approvals) == 1
        assert not dispatcher._runs
    finally:
        dispatcher.authority.release_selection(observer)
