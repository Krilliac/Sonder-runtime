"""Host reauthorization never reconstructs authority from a stored principal."""

from dataclasses import replace
import time

import pytest

from tests.test_delegated_verification import lanes


def granted(*args):
    from sonder_runtime.application.ports.lane_continuation import (
        GrantedApprovalEvidence,
    )

    return GrantedApprovalEvidence(
        "workspace_run",
        "a" * 64,
        "agent",
        "decision",
        "nonce",
        time.time() + 60,
        "approval",
    )


def make_host(lanes):
    from sonder_runtime.application.agents.lane_continuation import (
        LaneContinuationService,
    )
    from sonder_runtime.application.ports.lane_continuation import HostContinuationGrant

    service, store, model, root, context, parent = lanes
    context = replace(context, deadline_monotonic=time.monotonic() + 300)
    grant = HostContinuationGrant(
        context.principal_id,
        "host-task",
        "host-grant",
        1,
        time.time() + 600,
        (str(root),),
        tuple(sorted(service.allowed_tools)),
    )
    current = [grant]

    def authorize(ctx, host_id):
        if (
            ctx.principal_id != current[0].principal_id
            or host_id != current[0].host_conversation_id
        ):
            raise PermissionError("host selection unavailable")
        return current[0]

    host = LaneContinuationService(
        service, authorize_host=authorize, model_writable_roots=lambda: (root,)
    )
    return host, context, parent, current


def test_recovery_page_filters_selected_host_before_authorizing_rows(lanes):
    host, context, parent, current = make_host(lanes)
    selected = current[0]
    first = host.register_parent(parent['parent_session_id'], parent['parent_token'],
                                 'host-task', context=context, command_id='selected')
    first.close()
    current[0] = replace(selected, host_conversation_id='other-task')
    other_parent = lanes[0].open_model_parent(context)
    other = host.register_parent(other_parent['parent_session_id'], other_parent['parent_token'],
                                 'other-task', context=context, command_id='other')
    other.close()
    current[0] = selected
    page = host.recovery_page(context, host_conversation_id='host-task', limit=1)
    assert [item.continuation_id for item in page.items] == [first.continuation_id]
    assert page.has_more is False
    assert host.recovery_page(context, host_conversation_id='host-task',
                              cursor=page.next_cursor).items == ()
    with pytest.raises(PermissionError):
        host.recovery_page(context, host_conversation_id='other-task')


def test_registration_fences_old_bearer_and_raw_root_admission(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    bound.require_current()
    with pytest.raises(PermissionError):
        lanes[0].verify_model_parent(
            parent["parent_session_id"], parent["parent_token"], context
        )
    with pytest.raises(PermissionError):
        lanes[0].spawn(
            command_id="raw",
            parent_session_id=parent["parent_session_id"],
            task="raw attempt",
            workspace_root=str(lanes[3]),
            context=context,
        )
    assert lanes[2].calls == 0
    bound.close()


def test_explicit_reattachment_preserves_expiry_and_fences_closed_bound(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    identity = bound.continuation_id
    selected = host.select(identity, context)
    prepared = host.prepare_reattachment(selected, context, command_id="attach")
    with pytest.raises(PermissionError):
        host.execute_reattachment(prepared, context, approve=lambda p, c: "yes")
    bound.close()
    with pytest.raises(PermissionError):
        bound.require_current()
    fresh_context = replace(
        context, correlation_id="fresh", deadline_monotonic=time.monotonic() + 600
    )
    selected = host.select(identity, fresh_context)
    prepared = host.prepare_reattachment(selected, fresh_context, command_id="attach")
    new = host.execute_reattachment(prepared, fresh_context, approve=granted)
    new.require_current()
    assert prepared.expires_at < time.time() + 301
    assert lanes[2].calls == 0
    new.close()


def test_live_grant_reduction_and_missing_authorizer_fail_closed(lanes):
    from sonder_runtime.application.agents.lane_continuation import (
        LaneContinuationService,
    )

    host, context, parent, current = make_host(lanes)
    with pytest.raises(PermissionError):
        LaneContinuationService(lanes[0]).register_parent(
            parent["parent_session_id"],
            parent["parent_token"],
            "host-task",
            context=context,
            command_id="absent",
        )
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    current[0] = replace(current[0], workspace_roots=())
    with pytest.raises(PermissionError):
        bound.require_current()
    bound.close()


def test_recovery_is_bounded_read_only_metadata(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    page = host.recovery_page(context, limit=1)
    assert len(page.items) == 1
    assert page.items[0].continuation_id == bound.continuation_id
    assert "parent_token" not in repr(page)
    assert lanes[2].calls == 0
    with pytest.raises(ValueError):
        host.recovery_page(context, limit=129)
    bound.close()


def _reattach_process(
    database, session_database, root, identity, grant, ready, release
):
    from pathlib import Path
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )
    from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
    from sonder_runtime.application.agents.lane_continuation import (
        LaneContinuationService,
    )
    from sonder_runtime.application.context import local_owner_context

    sessions = SQLiteSessionRepository(Path(session_database))
    store = SQLiteAgentLaneStore(database, sessions)
    lanes_service = AgentLaneService(store, sessions, None, auto_start=False)
    host = LaneContinuationService(
        lanes_service,
        authorize_host=lambda ctx, name: grant,
        model_writable_roots=lambda: (Path(root),),
    )
    context = local_owner_context(
        correlation_id="separate-process",
        workspace_roots=(Path(root),),
        timeout_seconds=120,
    )
    prepared = host.prepare_reattachment(
        host.select(identity, context), context, command_id="child-attach"
    )
    bound = host.execute_reattachment(prepared, context, approve=granted)
    ready.put(bound._epoch)
    release.wait(20)
    bound.close()
    lanes_service.close()


def test_real_separate_process_reattachment_excludes_second_controller(lanes):
    import multiprocessing

    host, context, parent, current = make_host(lanes)
    original = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    identity = original.continuation_id
    original.close()
    mp = multiprocessing.get_context("spawn")
    ready, release = mp.Queue(), mp.Event()
    process = mp.Process(
        target=_reattach_process,
        args=(
            lanes[1].path,
            str(lanes[3].parent / "sessions.db"),
            str(lanes[3]),
            identity,
            current[0],
            ready,
            release,
        ),
    )
    process.start()
    try:
        assert ready.get(timeout=20) == 2
        fresh = host.prepare_reattachment(
            host.select(identity, context), context, command_id="loser"
        )
        calls = []
        with pytest.raises(PermissionError):
            host.execute_reattachment(
                fresh, context, approve=lambda p, c: calls.append("gate")
            )
        assert calls == []
        assert lanes[2].calls == 0
    finally:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_tampered_prepared_attachment_and_wrong_principal_do_not_reach_gate(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    bound.close()
    with pytest.raises(PermissionError):
        host.select(bound.continuation_id, replace(context, principal_id="other"))
    prepared = host.prepare_reattachment(
        host.select(bound.continuation_id, context), context, command_id="attach"
    )
    calls = []
    with pytest.raises(PermissionError):
        host.execute_reattachment(
            replace(prepared, workspace_roots=()),
            context,
            approve=lambda p, c: calls.append("gate"),
        )
    assert calls == []


def test_reattachment_pending_preserves_exact_payload_until_actual_grant(lanes):
    from sonder_runtime.application.ports.lane_continuation import (
        PendingApprovalEvidence,
        VerificationApprovalPending,
    )

    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    bound.close()
    identity = bound.continuation_id
    prepared = host.prepare_reattachment(
        host.select(identity, context), context, command_id="attach"
    )

    def pending(*args):
        raise VerificationApprovalPending(
            PendingApprovalEvidence(
                "workspace_run", "a" * 64, "agent", "a" * 16, time.time() + 60
            )
        )

    for _ in range(2):
        with pytest.raises(VerificationApprovalPending):
            host.execute_reattachment(prepared, context, approve=pending)
        replay = host.prepare_reattachment(
            host.select(identity, context), context, command_id="attach"
        )
        assert replay.approval_payload() == prepared.approval_payload()
    fresh = host.execute_reattachment(replay, context, approve=granted)
    fresh.require_current()
    assert lanes[2].calls == 0
    fresh.close()


def test_detaching_parent_does_not_cancel_independently_admitted_child(lanes):
    host, context, parent, current = make_host(lanes)
    child = lanes[0].spawn(
        command_id="child",
        parent_session_id=parent["parent_session_id"],
        task="Independent existing task",
        workspace_root=str(lanes[3]),
        context=context,
    )["lane"]["id"]
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    bound.close()
    lanes[0].run_pending(child, context)
    assert lanes[1].read_lane(child)["status"] == "completed"
    assert lanes[2].calls == 1


def test_all_managed_root_controls_and_verifier_entrypoints_refuse_raw_context(lanes):
    from tests.test_lane_pending_verification import setup_pending

    host, bound, context, verifier, prepared, identity, gateway = setup_pending(lanes)
    child = prepared.children[0][0]
    for operation in (
        lambda: lanes[0].inspect(child, context),
        lambda: lanes[0].send_message(
            child,
            command_id="raw-steer",
            content="change",
            author="parent",
            context=context,
        ),
        lambda: lanes[0].control(
            child, "cancel", command_id="raw-cancel", context=context
        ),
        lambda: verifier.prepare(
            prepared.parent_session_id,
            command_id="raw-verify",
            context=context,
            bound_parent_revision=1,
        ),
        lambda: verifier.inspect(
            prepared.parent_session_id,
            prepared.verification_id,
            context=context,
            bound_parent_revision=1,
        ),
        lambda: verifier.reconcile(
            prepared.parent_session_id,
            prepared.verification_id,
            context=context,
            bound_parent_revision=1,
        ),
    ):
        with pytest.raises(PermissionError):
            operation()
    assert (
        verifier.validate(
            prepared.parent_session_id,
            prepared.verification_id,
            context=context,
            bound_parent_revision=1,
        ).valid
        is False
    )
    assert gateway.calls == 0
    bound.close()


def test_recovery_pages_more_than_one_hundred_expired_bindings_without_mutation(lanes):
    host, context, parent, current = make_host(lanes)
    base = current[0]
    grants = {}
    host.authorize_host = lambda ctx, name: grants[name]
    identities = []
    for index in range(130):
        name = "host-task-" + str(index)
        grants[name] = replace(base, host_conversation_id=name)
        capability = parent if index == 0 else lanes[0].open_model_parent(context)
        bound = host.register_parent(
            capability["parent_session_id"],
            capability["parent_token"],
            name,
            context=context,
            command_id="register-" + str(index),
        )
        identities.append(bound.continuation_id)
        bound.close()
        with lanes[1].connect() as conn:
            conn.execute(
                "UPDATE agent_lane_parent_grants SET expires=? WHERE session_id=?",
                (time.time() - 1, capability["parent_session_id"]),
            )
    with lanes[1].connect() as conn:
        before = [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ]
    cursor, seen = 0, []
    for _ in range(5):
        page = host.recovery_page(context, cursor=cursor, limit=32)
        assert len(page.items) <= 32
        assert all(
            item.authority_state == "requires_reauthorization" for item in page.items
        )
        seen.extend(item.continuation_id for item in page.items)
        cursor = page.next_cursor
        if not page.has_more:
            break
    assert seen == identities and page.has_more is False
    with lanes[1].connect() as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM agent_lane_continuations")
        ] == before
    assert lanes[2].calls == 0


def test_fresh_broader_context_cannot_expand_original_root_or_cloud_ceiling(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    bound.close()
    extra = lanes[3].parent / "extra"
    extra.mkdir()
    fresh_context = replace(
        context,
        workspace_roots=(*context.workspace_roots, extra),
        cloud_allowed=True,
        remote_ollama_allowed=True,
    )
    current[0] = replace(
        current[0], workspace_roots=tuple(sorted((str(lanes[3]), str(extra))))
    )
    attachment = host.prepare_reattachment(
        host.select(bound.continuation_id, fresh_context),
        fresh_context,
        command_id="reattach",
    )
    fresh = host.execute_reattachment(attachment, fresh_context, approve=granted)
    command = object()
    root = [extra]

    class Commands:
        def decode_command(self, value):
            assert value is command
            return "spawn", dict(
                command_id="spawn", task="bounded", workspace_root=str(root[0])
            )

    host.command_codec = Commands()
    with pytest.raises(PermissionError):
        fresh.dispatch(command)
    root[0] = lanes[3]
    receipt = fresh.dispatch(command)
    stored = lanes[1].read_lane(receipt["lane"]["id"])
    assert stored["cloud_allowed"] is False
    assert stored["remote_ollama_allowed"] is False
    fresh.close()


def test_private_store_is_excluded_from_all_live_model_roots(lanes):
    host, context, parent, current = make_host(lanes)
    host.model_writable_roots = lambda: (lanes[3], lanes[3].parent)
    with pytest.raises(PermissionError, match="overlaps"):
        host.register_parent(
            parent["parent_session_id"],
            parent["parent_token"],
            "host-task",
            context=context,
            command_id="register",
        )
    host.model_writable_roots = lambda: (lanes[3],)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    host.model_writable_roots = lambda: (lanes[3].parent,)
    with pytest.raises(PermissionError, match="overlaps"):
        bound.require_current()
    bound.close()


def test_later_reattachment_uses_new_exact_call_without_reusing_prior_pending(lanes):
    from sonder_runtime.application.ports.delegated_verification import digest
    from sonder_runtime.application.ports.lane_continuation import (
        PendingApprovalEvidence,
        GrantedApprovalEvidence,
        VerificationApprovalPending,
    )

    host, context, parent, current = make_host(lanes)
    first = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    first.close()
    prepared = host.prepare_reattachment(
        host.select(first.continuation_id, context), context, command_id="first-attach"
    )
    call = digest(prepared.approval_payload())

    def pending_call(*args):
        raise VerificationApprovalPending(
            PendingApprovalEvidence(
                "workspace_run", call, "agent", call[:16], time.time() + 60
            )
        )

    with pytest.raises(VerificationApprovalPending):
        host.execute_reattachment(prepared, context, approve=pending_call)

    def approve(bundle, ctx):
        value = digest(bundle.approval_payload())
        return GrantedApprovalEvidence(
            "workspace_run",
            value,
            "agent",
            value,
            "nonce-" + value,
            time.time() + 60,
            "approval",
        )

    second = host.execute_reattachment(prepared, context, approve=approve)
    second.close()
    later = host.prepare_reattachment(
        host.select(first.continuation_id, context), context, command_id="later-attach"
    )
    assert digest(later.approval_payload()) != call
    third = host.execute_reattachment(later, context, approve=approve)
    third.require_current()
    third.close()


def test_parent_admission_guard_checks_actual_context_against_safe_ceiling(lanes):
    host, context, parent, current = make_host(lanes)
    bound = host.register_parent(
        parent["parent_session_id"],
        parent["parent_token"],
        "host-task",
        context=context,
        command_id="register",
    )
    ceiling = bound.authority_ceiling()
    narrowed = replace(context, deadline_monotonic=ceiling.deadline_monotonic)
    bound.require_current(context=narrowed)
    for changed in (
        replace(narrowed, cloud_allowed=True),
        replace(narrowed, deadline_monotonic=None),
        replace(narrowed, deadline_monotonic=float("nan")),
        replace(narrowed, workspace_roots=(lanes[3].parent,)),
    ):
        with pytest.raises(PermissionError):
            bound.require_current(context=changed)
    bound.close()
    with pytest.raises(PermissionError):
        bound.require_current(context=narrowed)
