"""Fresh selection can observe old work without reviving its admission."""

from dataclasses import replace

import pytest

from tests.test_app_control_store import state, enroll, select, OWNER
from tests.test_app_work_pending import running
from tests.test_app_managed_work_store import admit
from sonder_runtime.adapters.persistence.app_control import SQLiteAppControlStore
from sonder_runtime.application.ports.app_control import CommandConflict, NotFound


def recovered_selection(state):
    store, scope, pending = running(state)
    original = store.atomic(
        lambda tx: tx.record_work_verification_pending(
            **scope, expected_revision=4, pending=pending
        )
    )
    current = replace(
        state[1],
        control_session_id="session2",
        account_session_ref="account-session-v1:" + "e" * 64 + "." + "f" * 64,
    )
    enroll(store, current, command="enroll2")
    receipt = select(store, current, command="select2")
    return (
        store,
        original,
        dict(
            principal_id=OWNER,
            control_session_id="session2",
            binding_id="binding1",
            binding_revision=1,
            selection_id=receipt.entity_id,
            epoch=1,
            work_id="work1",
        ),
    )


def test_new_session_reads_pending_without_changing_original_or_admission(state):
    store, original, scope = recovered_selection(state)
    # Original work expires at 700; binding/current control remain valid.
    later = SQLiteAppControlStore(store.path, clock=lambda: 750)
    assert later.atomic(lambda tx: tx.read_recovery_work(**scope)) == original
    assert (
        later.atomic(
            lambda tx: tx.read_work(
                principal_id=OWNER, control_session_id="session2", work_id="work1"
            )
        )
        is None
    )
    with pytest.raises(CommandConflict):
        admit(later)
    assert (
        store.atomic(
            lambda tx: tx.read_work(
                principal_id=OWNER, control_session_id="session1", work_id="work1"
            )
        )
        == original
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(epoch=2),
        dict(binding_revision=2),
        dict(selection_id="other"),
        dict(control_session_id="missing"),
    ],
)
def test_recovery_history_requires_exact_live_selection(state, change):
    store, _, scope = recovered_selection(state)
    with pytest.raises((CommandConflict, NotFound)):
        store.atomic(lambda tx: tx.read_recovery_work(**dict(scope, **change)))


def test_recovery_history_does_not_find_other_work(state):
    store, _, scope = recovered_selection(state)
    assert (
        store.atomic(lambda tx: tx.read_recovery_work(**dict(scope, work_id="missing")))
        is None
    )


def test_recovery_history_refuses_expired_current_authority(state):
    store, _, scope = recovered_selection(state)
    later = SQLiteAppControlStore(store.path, clock=lambda: 901)
    with pytest.raises((CommandConflict, NotFound)):
        later.atomic(lambda tx: tx.read_recovery_work(**scope))


def test_recovery_history_hides_another_binding(state):
    from tests.test_app_control_store import create
    from sonder_runtime.application.ports.app_control import CommandKey

    store, original, scope = recovered_selection(state)
    create(store, state[1], bid="binding2", command="create2")
    receipt = store.atomic(
        lambda tx: tx.select_binding(
            CommandKey(OWNER, "control:session2", "switch2"),
            argument_digest="1" * 64,
            control_session_id="session2",
            binding_id="binding2",
            expected_binding_revision=1,
            expected_epoch=1,
        )
    )
    assert (
        store.atomic(
            lambda tx: tx.read_recovery_work(
                **dict(
                    scope,
                    binding_id="binding2",
                    selection_id=receipt.entity_id,
                    epoch=2,
                )
            )
        )
        is None
    )


def test_recovery_history_validates_retained_scalar_scope(state):
    from sonder_runtime.application.ports.app_control import StoreUnavailable

    store, _, scope = recovered_selection(state)
    store.atomic(
        lambda tx: tx._conn.execute(
            "UPDATE app_managed_work SET session='corrupted' WHERE id='work1'"
        )
    )
    with pytest.raises(StoreUnavailable):
        store.atomic(lambda tx: tx.read_recovery_work(**scope))


from tests.test_app_managed_authority import managed, control


def test_private_history_uses_actual_current_account_authority(managed):
    import time
    from sonder_runtime.application.ports.app_control import CommandKey
    from sonder_runtime.application.ports.app_managed_work import (
        PreparedAppWork,
        PreparedWorkbenchRun,
        WorkSpec,
    )
    from sonder_runtime.bootstrap.app_work_recovery import AppWorkRecoveryHistory

    authority, selection, lanes, model, *_ = managed
    now = time.time()
    prepared = PreparedAppWork(
        "history-work",
        CommandKey(
            selection.binding.principal_id,
            "control:" + selection.control.control_session_id,
            "prepare-history",
        ),
        selection.binding,
        selection.slot,
        selection.account.reference,
        PreparedWorkbenchRun(
            WorkSpec("Inspect", "code", "scripted", 1, False),
            selection.binding.grant.roots[0],
            ("scripted",),
            "1" * 64,
            False,
        ),
        "2" * 64,
        now,
        min(
            now + 60,
            selection.control.expires_at,
            selection.binding.expires_at,
            selection.account.expires_at,
        ),
    )
    original = authority.work_atomic(
        selection, selection.context, lambda tx: tx.prepare_work(prepared)
    )
    history = AppWorkRecoveryHistory(authority)
    assert history.inspect(selection, work_id="history-work") == original
    assert model.calls == 0
    authority.release_selection(selection)
    with pytest.raises(PermissionError):
        history.inspect(selection, work_id="history-work")
    assert model.calls == 0
