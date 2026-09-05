from dataclasses import replace
import pytest
from tests.test_app_control_store import state, OWNER
from tests.test_app_managed_work_store import preparation, admit
from sonder_runtime.application.ports.app_control import CommandConflict
from sonder_runtime.application.ports.host_turn_links import ManagedHostTurnLink


def test_run_link_is_durable_and_never_readmitted(state):
    store, work = preparation(state)
    store.atomic(lambda tx: tx.prepare_work(work))
    admit(store)
    scope = dict(
        principal_id=OWNER,
        control_session_id="session1",
        work_id="work1",
        dispatch_id="dispatch1",
        process_incarnation="process1",
    )
    bound = store.atomic(
        lambda tx: tx.bind_work_run(**scope, expected_revision=2, run_id="run1")
    )
    assert bound.state == "run_binding" and bound.revision == 3
    assert admit(store).record == bound and not admit(store).newly_admitted
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.bind_work_run(**scope, expected_revision=2, run_id="other")
        )
    link = ManagedHostTurnLink(
        "continuation1", "parent1", work.binding.canonical_host_id, OWNER, "run1", 1
    )
    with pytest.raises(ValueError):
        store.atomic(
            lambda tx: tx.bind_work_host(
                **scope,
                expected_revision=3,
                host_turn=replace(link, host_conversation_id="foreign")
            )
        )
    running = store.atomic(
        lambda tx: tx.bind_work_host(**scope, expected_revision=3, host_turn=link)
    )
    assert running.state == "running" and running.revision == 4
    assert running.host_turn == link
    assert admit(store).record == running and not admit(store).newly_admitted
    assert store.atomic(lambda tx: tx.prepare_work(work)) == running
    with pytest.raises(CommandConflict):
        admit(store, process_incarnation="restarted")


def test_link_cannot_admit_prepared_work_or_change_original_encoding(state):
    import json
    from dataclasses import asdict
    from sonder_runtime.adapters.persistence.app_control import _encode, _decode
    from sonder_runtime.application.ports.app_managed_work import AppWorkRecord

    store, work = preparation(state)
    record = store.atomic(lambda tx: tx.prepare_work(work))
    old = asdict(record)
    old.pop("run_id")
    old.pop("host_turn")
    raw = json.dumps(old, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert _encode(record) == raw
    assert _decode(raw, AppWorkRecord) == record
    with pytest.raises(CommandConflict):
        store.atomic(
            lambda tx: tx.bind_work_run(
                principal_id=OWNER,
                control_session_id="session1",
                work_id="work1",
                expected_revision=2,
                dispatch_id="dispatch1",
                process_incarnation="process1",
                run_id="run1",
            )
        )
    assert store.atomic(lambda tx: tx.prepare_work(work)) == record
