"""Delegated verification requires quiescence, live authority and source equality."""

from dataclasses import replace
from pathlib import Path
import pytest
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse


@pytest.fixture
def lanes(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)

    class Model:
        calls = 0

        def generate(self, request, context):
            self.calls += 1
            return ModelResponse("Completed", "scripted", "code", tokens_out=1)

    model = Model()
    service = AgentLaneService(store, sessions, model, auto_start=False)
    root = tmp_path / "repo"
    root.mkdir()
    context = local_owner_context(
        correlation_id="verification", workspace_roots=(root,)
    )
    parent = service.open_model_parent(context)
    return service, store, model, root, context, parent


def spawn(lanes):
    service, store, model, root, context, parent = lanes
    return service.spawn(
        command_id="spawn",
        parent_session_id=parent["parent_session_id"],
        task="Repair source",
        workspace_root=str(root),
        context=context,
    )["lane"]["id"]


def test_generation_tracks_accepted_work_but_not_command_replay(lanes):
    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    with store.transaction() as tx:
        first = tx.verification_generation(
            parent["parent_session_id"], context.principal_id
        )
    spawn(lanes)
    with store.transaction() as tx:
        assert (
            tx.verification_generation(
                parent["parent_session_id"], context.principal_id
            )
            == first
        )
    service.send_message(
        lane_id,
        command_id="steer",
        content="Include regression",
        author="user",
        context=context,
    )
    with store.transaction() as tx:
        assert (
            tx.verification_generation(
                parent["parent_session_id"], context.principal_id
            )
            > first
        )


def test_barrier_keeps_steering_visible_and_fences_runner(lanes):
    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    with store.transaction() as tx:
        tx.acquire_verification_barrier(
            parent["parent_session_id"], context.principal_id, "verify-1"
        )
    service.send_message(
        lane_id,
        command_id="steer",
        content="Fix edge case",
        author="user",
        context=context,
    )
    service.run_pending(lane_id, context)
    assert model.calls == 0
    assert any(
        m["content"] == "Fix edge case"
        for m in service.inspect(lane_id, context)["messages"]
    )
    with store.transaction() as tx:
        tx.release_verification_barrier(
            parent["parent_session_id"], context.principal_id, "verify-1"
        )
    service.run_pending(lane_id, context)
    assert model.calls == 1


def test_manifest_covers_untracked_files_and_refuses_bounds(tmp_path):
    from sonder_runtime.adapters.filesystem.workspace_manifest import (
        WorkspaceSnapshotter,
        ManifestLimits,
    )

    (tmp_path / "untracked.py").write_text("x = 1\n")
    snap = WorkspaceSnapshotter()
    before = snap.capture((tmp_path,))
    (tmp_path / "untracked.py").write_text("x = 2\n")
    assert snap.capture((tmp_path,)).digest != before.digest
    with pytest.raises(ValueError, match="bound"):
        WorkspaceSnapshotter(ManifestLimits(max_file_bytes=2)).capture((tmp_path,))


def test_certificate_requires_separate_exact_approval_and_invalidates_on_steering(
    lanes,
):
    from sonder_runtime.application.agents.delegated_verification import (
        DelegatedVerificationService,
    )
    from sonder_runtime.application.ports.delegated_verification import PreparedCheck
    from sonder_runtime.adapters.filesystem.workspace_manifest import (
        WorkspaceSnapshotter,
    )

    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    service.run_pending(lane_id, context)

    class Gateway:
        calls = 0

        def prepare_checks(self, roots):
            return (PreparedCheck("unit", "catalog", "argv", roots[0]),)

        def require_current(self, checks):
            assert checks == self.prepare_checks((str(root),))

        def execute_check(self, check, call_id, parent, context, *, permit):
            self.calls += 1
            proofs["lane-test-" + call_id] = dict(
                job_id="lane-test-" + call_id,
                parent_session_id=parent,
                principal_id=context.principal_id,
                process_exited=True,
                containment_empty=True,
                resources_released=True,
                exit_code=0,
                status="succeeded",
                job_revision=3,
                digest="proof",
            )

    proofs = {}
    gateway = Gateway()
    verifier = DelegatedVerificationService(
        service, gateway, proofs.get, WorkspaceSnapshotter()
    )
    prepared = verifier.prepare(
        parent["parent_session_id"],
        command_id="verify",
        context=context,
        bound_parent_revision=1,
    )
    assert gateway.calls == 0
    approvals = []

    def approve(bundle, ctx):
        approvals.append(bundle.approval_payload())
        return "operator-approval-1"

    result = verifier.execute_prepared(prepared, context=context, approve=approve)
    assert result["state"] == "certified"
    assert len(approvals) == 1 and gateway.calls == 1
    assert verifier.validate(
        parent["parent_session_id"],
        prepared.verification_id,
        context=context,
        bound_parent_revision=1,
    ).valid
    replay = verifier.execute_prepared(prepared, context=context, approve=approve)
    assert replay == result and gateway.calls == 1
    service.send_message(
        lane_id,
        command_id="new-work",
        content="Add case",
        author="user",
        context=context,
    )
    assert not verifier.validate(
        parent["parent_session_id"],
        prepared.verification_id,
        context=context,
        bound_parent_revision=1,
    ).valid


def _verifier(lanes, callback=None):
    from sonder_runtime.application.agents.delegated_verification import (
        DelegatedVerificationService,
    )
    from sonder_runtime.application.ports.delegated_verification import PreparedCheck
    from sonder_runtime.adapters.filesystem.workspace_manifest import (
        WorkspaceSnapshotter,
    )

    service, store, model, root, context, parent = lanes
    proofs = {}

    class Gateway:
        calls = 0
        current = True

        def prepare_checks(self, roots):
            return tuple(
                PreparedCheck("unit", "catalog", "argv", root) for root in roots
            )

        def require_current(self, checks):
            if not self.current:
                raise PermissionError("catalog changed")

        def execute_check(self, check, call_id, parent, context, *, permit):
            self.calls += 1
            if callback:
                callback(context)
            proofs["lane-test-" + call_id] = dict(
                job_id="lane-test-" + call_id,
                parent_session_id=parent,
                principal_id=context.principal_id,
                process_exited=True,
                containment_empty=True,
                resources_released=True,
                status="succeeded",
                exit_code=0,
                job_revision=3,
                digest="proof",
            )

    gateway = Gateway()
    verifier = DelegatedVerificationService(
        service, gateway, proofs.get, WorkspaceSnapshotter()
    )
    return verifier, gateway, proofs


def _prepared(lanes, verifier):
    *_, context, parent = lanes
    return verifier.prepare(
        parent["parent_session_id"],
        command_id="verification",
        context=context,
        bound_parent_revision=1,
    )


@pytest.mark.parametrize(
    "condition",
    ["owner", "pending_effect", "pending_response", "queued", "unproven_process"],
)
def test_verifier_refuses_terminal_status_without_complete_quiescence(lanes, condition):
    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    service.run_pending(lane_id, context)
    with store.transaction() as tx:
        lane = tx.lane(lane_id)
        if condition in {"owner", "pending_effect", "pending_response"}:
            lane[condition] = True if condition == "pending_effect" else "not-empty"
        elif condition == "queued":
            tx.message(lane, "unhandled", "user")
        else:
            tx.emit(
                lane,
                "tool.requested",
                {"name": "run_tests", "call_id": "unknown-process"},
            )
        tx.save(lane)
    verifier, gateway, _ = _verifier(lanes)
    with pytest.raises(ValueError):
        _prepared(lanes, verifier)
    assert gateway.calls == 0


@pytest.mark.parametrize("change", ["message", "catalog", "source", "grant"])
def test_change_during_independent_check_never_certifies(lanes, change):
    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    service.run_pending(lane_id, context)

    def mutate(execution_context):
        if change == "message":
            service.send_message(
                lane_id,
                command_id="steer",
                content="new work",
                author="user",
                context=context,
            )
            service.run_pending(lane_id, context)
            assert model.calls == 1
            assert execution_context.cancellation.cancelled
        elif change == "catalog":
            gateway.current = False
        elif change == "source":
            (root / "changed.py").write_text("new source")
        else:
            service.revoke_model_parent(
                parent["parent_session_id"], parent["parent_token"], context
            )

    verifier, gateway, _ = _verifier(lanes, mutate)
    prepared = _prepared(lanes, verifier)
    result = verifier.execute_prepared(
        prepared, context=context, approve=lambda *a: "approval"
    )
    assert result["state"] == "failed"
    assert result["certificate"] is None
    with store.transaction() as tx:
        assert not tx.verification_barrier(
            parent["parent_session_id"], context.principal_id
        )


def test_competing_store_cannot_steal_barrier_and_unresolved_cleanup_keeps_it(lanes):
    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, proofs = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    other = SQLiteAgentLaneStore(store.path, store.sessions)
    with other.transaction() as tx:
        with pytest.raises(ValueError, match="owned"):
            tx.acquire_verification_barrier(
                parent["parent_session_id"], context.principal_id, "competitor"
            )

    def uncertain(*args, **kwargs):
        raise OSError("process exit uncertain")

    gateway.execute_check = uncertain
    result = verifier.execute_prepared(
        prepared, context=context, approve=lambda *a: "approval"
    )
    assert result["state"] == "incomplete"
    with other.transaction() as tx:
        assert (
            tx.verification_barrier(parent["parent_session_id"], context.principal_id)
            == prepared.verification_id
        )
    with pytest.raises(ValueError, match="proof"):
        verifier.reconcile(
            parent["parent_session_id"],
            prepared.verification_id,
            context=context,
            bound_parent_revision=1,
        )


def test_approval_refusal_does_not_launch_and_cross_principal_cannot_inspect(lanes):
    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    result = verifier.execute_prepared(
        prepared, context=context, approve=lambda *a: None
    )
    assert result["state"] == "failed" and gateway.calls == 0
    with pytest.raises(PermissionError):
        verifier.inspect(
            parent["parent_session_id"],
            prepared.verification_id,
            context=replace(context, principal_id="other"),
            bound_parent_revision=1,
        )


def test_fresh_context_can_validate_but_cannot_spend_prepared_approval(lanes):
    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    result = verifier.execute_prepared(
        prepared, context=context, approve=lambda *a: "approval"
    )
    assert result["state"] == "certified"
    new_context = replace(context, correlation_id="reattached")
    assert verifier.validate(
        parent["parent_session_id"],
        prepared.verification_id,
        context=new_context,
        bound_parent_revision=1,
    ).valid


def test_manifest_refuses_link_and_counts_untracked_directory_entries(tmp_path):
    from sonder_runtime.adapters.filesystem.workspace_manifest import (
        WorkspaceSnapshotter,
        ManifestLimits,
    )

    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "source.py").write_text("x=1")
    with pytest.raises(ValueError, match="entry bound"):
        WorkspaceSnapshotter(ManifestLimits(max_entries=1)).capture((tmp_path,))
    try:
        (tmp_path / "alias").symlink_to(tmp_path / "one", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires OS privilege")
    with pytest.raises(ValueError, match="links|reparse"):
        WorkspaceSnapshotter().capture((tmp_path,))


@pytest.mark.parametrize("intent", [False, True])
def test_dead_controller_reconciles_without_replaying_unknown_dispatch(lanes, intent):
    import subprocess
    import sys

    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    script = """
import os, sys, uuid
from pathlib import Path
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
store = SQLiteAgentLaneStore(sys.argv[1], SQLiteSessionRepository(Path(sys.argv[1]).parent/'sessions.db'))
owner = 'lane-owner-' + uuid.uuid4().hex
lease = store.acquire_owner(owner)
with store.transaction() as tx:
    value = tx.verification_row(sys.argv[2], 'owner')
    value.update(owner=owner, state='running')
    if sys.argv[3] == 'True':
        value['job_ids'] = ['lane-test-unknown-dispatch']
    tx.save_verification(value)
os._exit(23)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            store.path,
            prepared.verification_id,
            str(intent),
        ],
        timeout=15,
    )
    assert result.returncode == 23
    if intent:
        with pytest.raises(ValueError, match="proof"):
            verifier.reconcile(
                parent["parent_session_id"],
                prepared.verification_id,
                context=context,
                bound_parent_revision=1,
            )
    else:
        recovered = verifier.reconcile(
            parent["parent_session_id"],
            prepared.verification_id,
            context=context,
            bound_parent_revision=1,
        )
        assert (
            recovered["state"] == "failed"
            and recovered["code"] == "RECOVERED_INCOMPLETE"
        )
    assert gateway.calls == 0
    with store.transaction() as tx:
        assert (
            bool(
                tx.verification_barrier(
                    parent["parent_session_id"], context.principal_id
                )
            )
            is intent
        )


def test_complete_child_query_sees_terminal_row_beyond_first_page(lanes):
    import json

    service, store, model, root, context, parent = lanes
    lane_id = spawn(lanes)
    service.run_pending(lane_id, context)
    with store.transaction() as tx:
        original = tx.lane(lane_id)
        for number in range(100):
            clone = dict(original, id="retained-" + str(number))
            tx.conn.execute(
                "INSERT INTO agent_lanes(id,principal,parent_session,data) VALUES (?,?,?,?)",
                (
                    clone["id"],
                    context.principal_id,
                    parent["parent_session_id"],
                    json.dumps(clone),
                ),
            )
        last = dict(original, id="last-retained", pending_effect=True)
        tx.conn.execute(
            "INSERT INTO agent_lanes(id,principal,parent_session,data) VALUES (?,?,?,?)",
            (
                last["id"],
                context.principal_id,
                parent["parent_session_id"],
                json.dumps(last),
            ),
        )
    verifier, gateway, _ = _verifier(lanes)
    with pytest.raises(ValueError, match="quiescent"):
        _prepared(lanes, verifier)
    assert gateway.calls == 0


def test_lost_certificate_commit_response_replays_exact_receipt(lanes, monkeypatch):
    from contextlib import contextmanager

    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    original = store.transaction
    failed = False

    @contextmanager
    def commit_then_fail():
        nonlocal failed
        with original() as tx:
            yield tx
            row = tx.verification_row(prepared.verification_id, context.principal_id)
            inject = row["state"] == "certified" and not failed
        if inject:
            failed = True
            raise OSError("lost commit acknowledgement")

    monkeypatch.setattr(store, "transaction", commit_then_fail)
    first = verifier.execute_prepared(
        prepared, context=context, approve=lambda *a: "approval"
    )
    assert failed and first["state"] == "certified"
    assert (
        verifier.execute_prepared(prepared, context=context, approve=lambda *a: None)
        == first
    )
    assert gateway.calls == 1


def test_prepared_bundle_tampering_cannot_spend_approval(lanes):
    service, store, model, root, context, parent = lanes
    service.run_pending(spawn(lanes), context)
    verifier, gateway, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    altered = replace(
        prepared, checks=(replace(prepared.checks[0], argv_digest="changed"),)
    )
    with pytest.raises(PermissionError, match="binding"):
        verifier.execute_prepared(
            altered, context=context, approve=lambda *a: "approval"
        )
    assert gateway.calls == 0


@pytest.mark.parametrize("foreign_principal", [False, True])
def test_foreign_queued_or_owned_overlap_refuses_verifier_admission(
    lanes, foreign_principal
):
    service, store, model, root, context, parent = lanes
    original = spawn(lanes)
    service.run_pending(original, context)
    service.control(original, "cancel", command_id="old-cancel", context=context)
    foreign_context = (
        replace(context, principal_id="account:foreign", auth_level="admin")
        if foreign_principal
        else context
    )
    service.authorize_grant = lambda lane, context: None
    other_parent = service.open_model_parent(foreign_context)
    other = service.spawn(
        command_id="other-child",
        parent_session_id=other_parent["parent_session_id"],
        task="Other work",
        workspace_root=str(root),
        context=foreign_context,
    )["lane"]["id"]
    verifier, gateway, _ = _verifier(lanes)
    with pytest.raises(ValueError, match="overlap"):
        _prepared(lanes, verifier)
    with store.transaction() as tx:
        lane = tx.lane(other)
        lane.update(status="completed", owner="live-owner", pending_effect=True)
        tx.save(lane)
    with pytest.raises(ValueError, match="overlap"):
        _prepared(lanes, verifier)
    assert gateway.calls == 0


def test_two_parent_verifier_admission_race_has_one_workspace_owner(lanes):
    from concurrent.futures import ThreadPoolExecutor

    service, store, model, root, context, parent = lanes
    first_lane = spawn(lanes)
    service.run_pending(first_lane, context)
    service.control(first_lane, "cancel", command_id="cancel-first", context=context)
    second_parent = service.open_model_parent(context)
    second_lane = service.spawn(
        command_id="second",
        parent_session_id=second_parent["parent_session_id"],
        task="Other work",
        workspace_root=str(root),
        context=context,
    )["lane"]["id"]
    service.run_pending(second_lane, context)
    service.control(second_lane, "cancel", command_id="cancel-second", context=context)
    left, _, _ = _verifier(lanes)
    right, _, _ = _verifier(lanes)

    def prepare(verifier, parent_id, command):
        try:
            return verifier.prepare(
                parent_id, command_id=command, context=context, bound_parent_revision=1
            )
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(prepare, left, parent["parent_session_id"], "left"),
            pool.submit(prepare, right, second_parent["parent_session_id"], "right"),
        ]
        winners = [call.result() for call in calls]
    assert sum(winner is not None for winner in winners) == 1
