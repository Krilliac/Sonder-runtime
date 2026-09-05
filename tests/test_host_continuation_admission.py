from dataclasses import replace

import pytest

from tests.test_delegated_verification import lanes
from tests.test_lane_continuation import make_host


def admission(lanes):
    from sonder_runtime.bootstrap.standalone_continuation import HostContinuationAdmission

    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(parent['parent_session_id'], parent['parent_token'],
        'host-task', context=context, command_id='register')
    paths = [lanes[3].parent / 'approvals.db']
    roots = [lanes[3]]
    guard = HostContinuationAdmission(bound, context,
        private_paths=lambda: tuple(paths), model_writable_roots=lambda: tuple(roots))
    return guard, bound, context, current, paths, roots


def test_actual_bound_context_and_dispatch_are_fenced(lanes):
    guard, bound, context, current, paths, roots = admission(lanes)
    observed = []
    guard.invoke(lambda ctx: observed.append(ctx))
    assert observed[0].cancellation is context.cancellation
    assert observed[0].principal_id == context.principal_id
    assert observed[0].deadline_monotonic <= context.deadline_monotonic
    bound.close()
    with pytest.raises(PermissionError):
        guard.invoke(lambda ctx: observed.append(ctx))
    assert len(observed) == 1


def test_live_host_revocation_prevents_next_effect(lanes):
    guard, bound, context, current, paths, roots = admission(lanes)
    current[0] = replace(current[0], workspace_roots=())
    with pytest.raises(PermissionError):
        guard.invoke(lambda ctx: pytest.fail('effect ran after revocation'))
    bound.close()


@pytest.mark.parametrize('change', ['private_relocation', 'global_expansion', 'missing_inventory'])
def test_private_store_inventory_is_live_and_checks_all_roots(lanes, change):
    guard, bound, context, current, paths, roots = admission(lanes)
    if change == 'private_relocation':
        paths[:] = [lanes[3] / 'nested' / 'approvals.db']
    elif change == 'global_expansion':
        # Not present in the narrowed parent context.
        roots.append(lanes[3].parent)
    else:
        paths.clear()
    with pytest.raises(PermissionError):
        guard.invoke(lambda ctx: pytest.fail('private store exposed'))
    bound.close()


def test_returned_context_cannot_replace_identity_or_cancellation(lanes):
    guard, bound, context, current, paths, roots = admission(lanes)
    forged = replace(guard.context, principal_id='another-owner')
    with pytest.raises(PermissionError):
        guard.require_current(context=forged)
    bound.close()


def test_composition_never_expands_fresh_narrower_workspace(lanes):
    from sonder_runtime.bootstrap.standalone_continuation import HostContinuationAdmission

    guard, bound, context, current, paths, roots = admission(lanes)
    narrower = lanes[3] / 'subproject'
    narrower.mkdir()
    narrowed = HostContinuationAdmission(bound,
        replace(context, workspace_roots=(narrower,)),
        private_paths=lambda: tuple(paths), model_writable_roots=lambda: tuple(roots))
    assert narrowed.context.workspace_roots == (narrower,)
    bound.close()
