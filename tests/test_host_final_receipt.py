from types import SimpleNamespace
import pytest

from tests.test_delegated_verification import lanes
from tests.test_managed_standalone_session import setup
from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.application.ports.host_final import HostFinalFacts


def test_final_receipt_is_distinct_exact_immutable_and_required(lanes):
    app = object()
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda c, a: setup(lanes, c)[0], require_current=lambda: None)
    view = lifetime.factory(SimpleNamespace(run_id='final-receipt-turn'), app)
    turn_link = view.turn_link()
    draft = HostTerminalDraft(HostObservationLedger(project_scope=str(lanes[3])).seal(),
                              'model claims finished', 'NORMAL', ())
    facts = HostFinalFacts((), str(lanes[3]), True, False, False, 'VALIDATION_FAILED',
                          delegated_work=True)
    output = 'VALIDATION_FAILED: tests not run\n\nmodel claims finished'
    try:
        view.capture_terminal(draft)
        view.capture_final(output, facts)
        view.capture_final(output, facts)
        with pytest.raises(PermissionError):
            view.capture_final('different output', facts)
        bound = view._session._bound
        with lanes[1].transaction() as tx:
            record = bound._service._row(tx, bound.continuation_id)
            turn = record['host_turn']
            final = tx.terminal_projection(record['id'], 'owner', turn['final_receipt']['projection_id'])
            from sonder_runtime.application.ports.lane_continuation import open_projection
            decoded = open_projection(bound._service.projection_codec, final, final.binding)
            assert decoded.output == output
            assert turn['final_receipt']['facts']['validation_passed'] is False
            assert turn['final_receipt']['facts']['delegated_work'] is True
            assert turn['final_receipt']['original_digest'] == turn['projection_digest']
        view.close()
        evidence = lifetime.final_evidence(turn_link)
        assert evidence.result.output == output
        assert evidence.facts.delegated_work is True
        assert lifetime._owner.final_evidence(turn_link) == evidence
        next_view = lifetime.factory(SimpleNamespace(run_id='next-turn'), app)
        assert next_view is not view
        with pytest.raises(PermissionError):
            lifetime.final_evidence(turn_link)
    finally:
        lifetime.close()


def test_legacy_final_facts_do_not_imply_absence_of_delegation():
    legacy = dict(tools=(), project_scope="project", mutation_observed=False,
        validation_attempted=False, validation_passed=False, terminal_class="NORMAL")
    assert HostFinalFacts(**legacy).delegated_work is None
    assert HostFinalFacts(**legacy, delegated_work=False).delegated_work is False
    with pytest.raises(ValueError):
        HostFinalFacts(**legacy, delegated_work=0)


def test_original_capture_alone_cannot_close_turn(lanes):
    app = object()
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda c, a: setup(lanes, c)[0], require_current=lambda: None)
    try:
        view = lifetime.factory(SimpleNamespace(run_id='missing-final'), app)
        view.capture_terminal(HostTerminalDraft(
            HostObservationLedger(project_scope=str(lanes[3])).seal(), 'draft', 'NORMAL', ()))
        with pytest.raises(PermissionError, match='final'):
            view.close()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id='next'), app)
    finally:
        lifetime.close()


@pytest.mark.parametrize('damage', ['missing', 'facts', 'output'])
def test_corrupted_final_cannot_close_or_advance(lanes, damage):
    app = object()
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda c, a: setup(lanes, c)[0], require_current=lambda: None)
    try:
        view = lifetime.factory(SimpleNamespace(run_id='corrupt-final'), app)
        view.capture_terminal(HostTerminalDraft(
            HostObservationLedger(project_scope=str(lanes[3])).seal(), 'original', 'NORMAL', ()))
        view.capture_final('UNVERIFIED: original', HostFinalFacts(
            (), str(lanes[3]), False, False, False, 'UNVERIFIED'))
        bound = view._session._bound
        with lanes[1].transaction() as tx:
            record = bound._service._row(tx, bound.continuation_id)
            receipt = record['host_turn']['final_receipt']
            if damage == 'missing':
                del record['host_turn']['final_receipt']
            elif damage == 'facts':
                receipt['facts']['validation_passed'] = True
            else:
                receipt['projection_digest'] = '0' * 64
            bound._service._save(tx, record)
        with pytest.raises(PermissionError):
            view.close()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id='next'), app)
    finally:
        lifetime.close()


def test_outer_boundary_is_required_and_persistence_failure_stays_fenced(lanes, monkeypatch):
    import sonder_runtime.bootstrap.managed_conversation as composition
    app = object()
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda c, a: setup(lanes, c)[0], require_current=lambda: None)
    try:
        view = lifetime.factory(SimpleNamespace(run_id='outer-final'), app)
        view.capture_terminal(HostTerminalDraft(
            HostObservationLedger(project_scope=str(lanes[3])).seal(), 'original', 'NORMAL', ()))
        view.stage_final(HostFinalFacts((), str(lanes[3]), False, False, False, 'NORMAL'))
        view.close()
        with pytest.raises(PermissionError):
            view.require_current()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id='premature'), app)
        capture = composition.capture_host_final
        def failure(*args, **kwargs):
            raise OSError('final store unavailable')
        monkeypatch.setattr(composition, 'capture_host_final', failure)
        with pytest.raises(OSError):
            lifetime.finalize_result('original\n\nexact outer report')
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id='still-premature'), app)
        monkeypatch.setattr(composition, 'capture_host_final', capture)
        assert lifetime.finalize_result('original\n\nexact outer report') == 'original\n\nexact outer report'
        lifetime.factory(SimpleNamespace(run_id='admitted-next'), app)
    finally:
        lifetime.close()
