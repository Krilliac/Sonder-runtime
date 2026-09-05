import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.test_delegated_verification import lanes, _verifier
from tests.test_managed_standalone_session import setup, command
from tests.test_lane_coding_acceptance import coding, make_service, tool
from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.application.ports.host_final import HostFinalFacts


def final_receipt(view, output, terminal_class='NORMAL', *, validated=False):
    view.capture_final(output, HostFinalFacts((), str(view.context.workspace_roots[0]),
        False, validated, validated, terminal_class))


def test_typed_turn_and_terminal_links_bind_exact_outward_result(lanes):
    from sonder_runtime.bootstrap.managed_conversation import ManagedConversationLifetime
    app = object()
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda c, a: setup(lanes, c)[0], require_current=lambda: None)
    try:
        view = lifetime.factory(SimpleNamespace(run_id="linked-turn"), app)
        link = view.turn_link()
        assert link.run_id == "linked-turn" and link.ordinal == 1
        assert link.parent_session_id == view.report_metadata()["parent_session_id"]
        ledger = HostObservationLedger(project_scope=str(lanes[3]))
        view.capture_terminal(HostTerminalDraft(ledger.seal(), "original", "NORMAL", ()))
        view.stage_final(HostFinalFacts((), str(lanes[3]), False, False, False, "NORMAL"))
        view.close()
        with pytest.raises(PermissionError):
            view.turn_link()
        result = lifetime.finalize_result_with_receipt("original\nFinal report")
        assert result.output == "original\nFinal report"
        assert result.receipt.turn == link
        assert result.receipt.output_digest == hashlib.sha256(result.output.encode()).hexdigest()
        assert result.receipt.original_projection_digest != result.receipt.final_projection_digest
        assert lifetime.terminal_receipt("linked-turn", 1) == result.receipt
        assert lifetime.terminal_result("linked-turn", 1) == result
        with pytest.raises(PermissionError):
            lifetime.terminal_receipt("linked-turn", 2)
        with pytest.raises(PermissionError):
            lifetime.finalize_result_with_receipt("unrelated replacement")
        with lanes[1].transaction() as tx:
            owner = lifetime._owner
            stored = owner._host._row(tx, owner._bound.continuation_id)
            stored['host_turn']['final_receipt']['digest'] = '0' * 64
            owner._host._save(tx, stored)
        with pytest.raises(PermissionError):
            lifetime.terminal_receipt("linked-turn", 1)
        with pytest.raises(PermissionError):
            lifetime.terminal_result("linked-turn", 1)
    finally:
        lifetime.close()


@pytest.mark.parametrize('failure_after_release', [False, True])
def test_failed_owner_close_retains_fenced_retry_handle(lanes, monkeypatch, failure_after_release):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime, ReplConversationSlot,
    )
    app = object()
    owner = setup(lanes)[0]
    lifetime = ManagedConversationLifetime(application=app,
        session_factory=lambda controller, application: owner, require_current=lambda: None)
    view = lifetime.factory(SimpleNamespace(run_id='closing-turn'), app)
    slot = ReplConversationSlot()
    slot.select('original')
    slot.install(lambda callback: callback(), lifetime.close)
    close = owner.close
    calls = []
    def flaky_close():
        calls.append(owner)
        if len(calls) == 1:
            if failure_after_release:
                close()
            raise OSError('injected close failure')
        close()
    monkeypatch.setattr(owner, 'close', flaky_close)
    try:
        with pytest.raises(OSError):
            slot.clear()
        assert owner._bound._lease.handle.closed is failure_after_release
        with pytest.raises(PermissionError):
            view.require_current()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id='replacement'), app)
        with pytest.raises(PermissionError):
            slot.run(lambda: pytest.fail('closed slot ran'))
        with pytest.raises(PermissionError):
            slot.select('replacement')
        with pytest.raises(PermissionError):
            slot.install(lambda callback: callback(), lambda: None)
        assert len(calls) == 1
        slot.clear()
        assert calls == [owner, owner]
        assert owner._bound._lease.handle.closed
        assert slot.select('replacement') is False
    finally:
        close()


@pytest.mark.parametrize("damage", [None, "receipt", "cleanup", "projection"])
def test_two_certified_turns_keep_parent_and_original_receipts(lanes, damage):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )

    app = object()
    created = []

    def create(controller, application):
        owner, _, _, _ = setup(lanes, controller)
        created.append(owner)
        return owner

    lifetime = ManagedConversationLifetime(
        application=app, session_factory=create, require_current=lambda: None
    )
    verifier, gateway, proofs = _verifier(lanes)
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
    identities, parents = [], []
    try:
        for ordinal in range(2):
            controller = SimpleNamespace(run_id=f"host-turn-{ordinal}")
            view = lifetime.factory(controller, app)
            if ordinal == 0:
                child = view.dispatch(
                    command(
                        view,
                        controller,
                        "spawn",
                        {
                            "command_id": "child",
                            "task": "inspect",
                            "workspace_root": str(lanes[3]),
                        },
                    )
                )["lane"]
                lanes[0].run_pending(child["id"], view.context)
            ledger = view.inherit_host_ledger(
                HostObservationLedger(project_scope=str(lanes[3]))
            )
            draft = HostTerminalDraft(
                ledger.seal(), f"exact final {ordinal}", "NORMAL", ()
            )
            view.capture_terminal(draft)
            verdict = view.verify_delegated(
                draft, verifier_factory=lambda *args: verifier
            )
            assert verdict.valid
            identities.append(view._session._bound.pending_verification())
            parents.append(view.report_metadata()["parent_session_id"])
            final_receipt(view, draft.output, validated=True)
            view.close()
            with pytest.raises(PermissionError):
                view.require_current()
            if ordinal == 0 and damage:
                if damage == "cleanup":
                    proofs.clear()
                else:
                    with lanes[1].transaction() as tx:
                        if damage == "receipt":
                            tx.conn.execute(
                                "UPDATE agent_lane_terminal_results SET digest=?",
                                ("0" * 64,),
                            )
                        else:
                            record = created[0]._host._row(
                                tx, created[0]._bound.continuation_id
                            )
                            record["host_turn"]["projection_digest"] = "0" * 64
                            created[0]._host._save(tx, record)
                with pytest.raises(PermissionError):
                    lifetime.factory(SimpleNamespace(run_id="refused-next-turn"), app)
                assert created[0]._bound.pending_verification() == identities[0]
                return
        assert parents[0] == parents[1]
        assert identities[0].verification_id != identities[1].verification_id
        assert len(created) == 1 and gateway.calls == 2
        with lanes[1].transaction() as tx:
            assert (
                tx.terminal_projection(
                    created[0]._bound.continuation_id,
                    "owner",
                    identities[0].verification_id,
                ).sha256
                == identities[0].projection_digest
            )
            record = created[0]._host._row(tx, created[0]._bound.continuation_id)
            assert len(record["host_turn_history"]) == 1
            assert (
                record["host_turn_history"][0]["pending_identity"]["verification_id"]
                == identities[0].verification_id
            )
    finally:
        lifetime.close()
        reopened = type(lanes[1])(lanes[1].path, lanes[1].sessions)
        with reopened.transaction() as tx:
            retained = created[0]._host._row(tx, created[0]._bound.continuation_id)
            assert retained["host_turn"]["run_id"].startswith("host-turn-")


@pytest.mark.parametrize("unknown", [False, True])
def test_real_pending_or_consumed_unknown_cannot_advance(lanes, unknown):
    import time
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.application.ports.lane_continuation import (
        VerificationApprovalPending,
    )
    from tests.test_continuation_approval_bridge import bridge

    app = object()
    ledger = ApprovalLedger(lanes[3].parent / "turn-approvals.db")
    gate = bridge(ledger)
    approvals = []

    def create(controller, application):
        owner, _, _, _ = setup(lanes, controller)

        def approve(prepared, context):
            approvals.append(prepared.verification_id)
            kwargs = dict(
                surface="agent",
                expires_at=time.time() + min(120, context.remaining_seconds),
            )
            try:
                return gate.authorize(
                    "workspace_run", prepared.approval_payload(), **kwargs
                )
            except VerificationApprovalPending as pending:
                if not unknown:
                    raise
                row = ledger.resolve_call(pending.evidence.call_digest)
                ledger.issue(row.tool, row.digest, approver="operator")
                gate.authorize("workspace_run", prepared.approval_payload(), **kwargs)
                raise RuntimeError("lost approved response")

        owner._approve = approve
        return owner

    lifetime = ManagedConversationLifetime(
        application=app, session_factory=create, require_current=lambda: None
    )
    controller = SimpleNamespace(run_id="pending-turn")
    try:
        view = lifetime.factory(controller, app)
        child = view.dispatch(
            command(
                view,
                controller,
                "spawn",
                {
                    "command_id": "child",
                    "task": "inspect",
                    "workspace_root": str(lanes[3]),
                },
            )
        )["lane"]
        lanes[0].run_pending(child["id"], view.context)
        verifier, gateway, _ = _verifier(lanes)
        draft = HostTerminalDraft(
            HostObservationLedger(project_scope=str(lanes[3])).seal(),
            "exact pending",
            "NORMAL",
            (),
        )
        view.capture_terminal(draft)
        assert not view.verify_delegated(
            draft, verifier_factory=lambda *args: verifier
        ).valid
        identity = view._session._bound.pending_verification()
        final_receipt(view, 'UNVERIFIED: exact pending', 'UNVERIFIED')
        view.close()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id="new-turn"), app)
        assert lifetime._owner._bound.pending_verification() == identity
        assert len(approvals) == 1 and gateway.calls == 0
    finally:
        lifetime.close()


def test_dirty_failed_no_child_turn_is_retained_and_stale_view_fenced(lanes):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )

    app = object()
    live = [True]

    def guard():
        if not live[0]:
            raise PermissionError("selection revoked")

    lifetime = ManagedConversationLifetime(
        application=app,
        session_factory=lambda c, a: setup(lanes, c)[0],
        require_current=guard,
    )
    try:
        first = lifetime.factory(SimpleNamespace(run_id="failed-turn"), app)
        ledger = HostObservationLedger(project_scope=str(lanes[3]))
        ledger.observe(
            tool="file_write",
            arguments={"path": "changed"},
            observation="failed after effect",
            dispatched=True,
            success=False,
            dirty=True,
        )
        first.capture_terminal(
            HostTerminalDraft(ledger.seal(), "ERROR: incomplete", "ERROR", ())
        )
        final_receipt(first, 'ERROR: incomplete', 'ERROR')
        first.close()
        second = lifetime.factory(SimpleNamespace(run_id="repair-turn"), app)
        inherited = second.inherit_host_ledger(
            HostObservationLedger(project_scope=str(lanes[3]))
        )
        assert (
            inherited.resolve().dirty and not inherited.resolve().parent_effects_valid
        )
        with pytest.raises(PermissionError):
            first.dispatch(command(first._session, first._controller, "list", {}))
        live[0] = False
        with pytest.raises(PermissionError):
            second.report_metadata()
    finally:
        lifetime.close()


def test_missing_terminal_evidence_and_foreign_app_refuse_new_turn(lanes):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )

    app = object()
    lifetime = ManagedConversationLifetime(
        application=app,
        session_factory=lambda c, a: setup(lanes, c)[0],
        require_current=lambda: None,
    )
    try:
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id="foreign"), object())
        first = lifetime.factory(SimpleNamespace(run_id="unfinished"), app)
        with pytest.raises(PermissionError):
            first.close()
        with pytest.raises(PermissionError):
            lifetime.factory(SimpleNamespace(run_id="next"), app)
    finally:
        lifetime.close()


def test_two_turns_use_real_catalog_processes_and_released_job_proofs(coding):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )
    from sonder_runtime.bootstrap.delegated_verification import (
        compose_delegated_verification,
    )
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from tests.test_continuation_approval_bridge import bridge
    import time

    repo, catalog_path, store, sessions, jobs, provider, facade, context = coding
    service, model = make_service(
        coding,
        [
            tool("run_tests", target="unit"),
            tool(
                "edit_file",
                path="calc.py",
                old="return sum(values) + 1",
                new="return sum(values)",
            ),
            tool("run_tests", target="unit"),
            "Repaired",
        ],
    )
    independent = catalog_path.with_name("independent-turn-tests.json")
    catalog = json.loads(catalog_path.read_text())
    catalog["targets"] = [
        target for target in catalog["targets"] if target["name"] == "unit"
    ]
    independent.write_text(json.dumps(catalog))
    env = (service, store, model, repo, context, None)
    gate = bridge(
        ApprovalLedger(repo.parent / "turn-policy.db"),
        rule={"action": "allow", "pattern": "workspace_run"},
    )
    app = object()

    def create(controller, application):
        owner = setup(env, controller)[0]
        owner._approve = lambda prepared, admitted: gate.authorize(
            "workspace_run",
            prepared.approval_payload(),
            surface="agent",
            expires_at=time.time() + min(120, admitted.remaining_seconds),
        )
        return owner

    lifetime = ManagedConversationLifetime(
        application=app, session_factory=create, require_current=lambda: None
    )
    certificates = []
    try:
        for index in range(2):
            controller = SimpleNamespace(run_id=f"process-turn-{index}")
            view = lifetime.factory(controller, app)
            if index == 0:
                child = view.dispatch(
                    command(
                        view,
                        controller,
                        "spawn",
                        {
                            "command_id": "repair",
                            "task": "repair and test",
                            "workspace_root": str(repo),
                            "max_steps": 8,
                        },
                    )
                )["lane"]
                service.run_pending(child["id"], view.context)
                assert (
                    view.dispatch(
                        command(view, controller, "inspect", {"lane_id": child["id"]})
                    )["lane"]["status"]
                    == "completed"
                )
            ledger = view.inherit_host_ledger(
                HostObservationLedger(project_scope=str(repo))
            )
            draft = HostTerminalDraft(
                ledger.seal(), f"Reviewable repaired source {index}", "NORMAL", ()
            )
            view.capture_terminal(draft)
            verdict = view.verify_delegated(
                draft,
                verifier_factory=lambda *args: compose_delegated_verification(
                    service, provider, independent
                ),
            )
            assert verdict.valid
            certificates.append(verdict.certificate_id)
            with store.transaction() as tx:
                verification = tx.verification_row(
                    verdict.certificate_id, context.principal_id
                )
                assert verification["job_ids"]
                for job_id in verification["job_ids"]:
                    proof = provider.cleanup_proof(job_id)
                    assert all(
                        proof[key]
                        for key in (
                            "process_exited",
                            "containment_empty",
                            "resources_released",
                        )
                    )
            final_receipt(view, draft.output, validated=True)
            view.close()
        assert len(set(certificates)) == 2
        assert "return sum(values)\n" in (repo / "calc.py").read_text()
    finally:
        lifetime.close()


def test_turn_history_bound_refuses_without_evicting(lanes):
    from sonder_runtime.bootstrap.managed_conversation import (
        ManagedConversationLifetime,
    )

    app = object()
    lifetime = ManagedConversationLifetime(
        application=app,
        session_factory=lambda c, a: setup(lanes, c)[0],
        require_current=lambda: None,
    )
    try:
        for index in range(32):
            view = lifetime.factory(
                SimpleNamespace(run_id=f"bounded-turn-{index}"), app
            )
            ledger = view.inherit_host_ledger(
                HostObservationLedger(project_scope=str(lanes[3]))
            )
            view.capture_terminal(
                HostTerminalDraft(ledger.seal(), f"output {index}", "NORMAL", ())
            )
            final_receipt(view, f'output {index}')
            view.close()
        with pytest.raises(ValueError, match="limit"):
            lifetime.factory(SimpleNamespace(run_id="over-limit"), app)
        with lanes[1].transaction() as tx:
            record = lifetime._owner._host._row(
                tx, lifetime._owner._bound.continuation_id
            )
            assert len(record["host_turn_history"]) == 31
            assert record["host_turn"]["ordinal"] == 32
            assert record["host_turn_history"][0]["run_id"] == "bounded-turn-0"
    finally:
        lifetime.close()
