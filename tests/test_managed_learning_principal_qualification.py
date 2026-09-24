"""Live principal-independence qualification for managed verifier learning.

This is deliberately an end-to-end composition test: both principals are
registered and enrolled through app-control, each run goes through the managed
dispatcher and delegated verifier, and observations/promotion use the composed
application UnitOfWork. The verifier process boundary is represented by the
existing gateway double; no host-verifier authority is forged here.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import admin_auth
import server
from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
from sonder_runtime.adapters.persistence.sqlite.verifier_observations import (
    SQLiteVerifierObservationRepository,
)
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.lane_continuation import GrantedApprovalEvidence
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.bootstrap.app_managed_work import (
    AppManagedWorkDispatcher,
    dispatch_approval_digest,
)
from sonder_runtime.bootstrap.managed_conversation import (
    ManagedConversationLifetime,
    _ManagedTurn,
)
from sonder_runtime.bootstrap.managed_learning import ManagedLearningRecorder
from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession
from sonder_runtime.bootstrap.prepared_workbench import PreparedWorkbenchAdapter
from sonder_runtime.platform.memory_replication_config import MemoryReplicationConfig
from tests.test_app_control_http import control, invoke  # noqa: F401
from tests.test_app_managed_authority import Cancel
from tests.test_authoritative_memory_source import _live_replication_config
from tests.test_continuation_approval_bridge import bridge
from tests.test_delegated_verification import _verifier
from tests.test_tier_escalation import _install_agent_fakes


def _rows(path):
    from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter

    with UnitOfWorkAdapter(path) as scope:
        return SQLiteVerifierObservationRepository(scope.connection).list_pairs(
            limit=100
        )


def _selection_for(
    binding, *, account_token, credential, principal, roots, command_id, expected_epoch,
):
    binding_id = invoke(
        binding,
        account_token,
        "create_binding",
        {"command_id": command_id + "-create"},
        credential,
    )[1]["receipt"]["entity_id"]
    invoke(
        binding,
        account_token,
        "select_binding",
        {
            "command_id": command_id + "-select",
            "binding_id": binding_id,
            "expected_binding_revision": 1,
            "expected_epoch": expected_epoch,
        },
        credential,
    )
    return binding.issue_selection(
        account_token=account_token,
        control_token=credential,
        context=OperationContext(
            command_id,
            "account:" + hashlib.sha256(principal.encode()).hexdigest(),
            "admin",
            "http",
            time.monotonic() + 300,
            Cancel(),
            tuple(roots),
        ),
    )


def _dispatcher(authority, binding, application, monkeypatch):
    """The production dispatcher/lifetime wiring, with no synthetic authority."""
    _install_agent_fakes(monkeypatch, {"m-code": '{"final":"inspected repository"}'})
    workbench = PreparedWorkbenchAdapter(
        server,
        policy_snapshot=lambda: {
            "allowed_tools": ["file_read"], "allow_web": False,
            "allow_location": False, "revision": 1,
        },
    )

    def lifetime_factory(selected):
        def require():
            authority.work_atomic(selected, selected.context, lambda tx: None)

        def session(controller, owned_application):
            host = authority.continuation_service(
                selected, projection_codec=TerminalProjectionCodec(),
            )
            return ManagedStandaloneSession(
                controller=controller,
                application=owned_application,
                host=host,
                context=selected.context,
                host_conversation_id=selected.host_conversation_id,
                private_paths=lambda: (binding.store.path,),
                model_writable_roots=lambda: tuple(selected.context.workspace_roots),
                approve=lambda *args: approval(None, selected.context),
            )

        return ManagedConversationLifetime(
            application=application,
            session_factory=session,
            require_current=require,
        )

    def approval(work, context):
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

    return AppManagedWorkDispatcher(
        authority,
        workbench,
        application=application,
        lifetime_factory=lifetime_factory,
        authorize_dispatch=approval,
        terminal_eligibility=lambda *args: (_ for _ in ()).throw(
            PermissionError("verifier must be composed by the managed turn")
        ),
    )


def test_live_principal_qualification_promotes_then_failed_check_demotes_after_restart(
    control, monkeypatch, tmp_path_factory,  # noqa: F811 - imported pytest fixture
):
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )
    from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
    from sonder_runtime.bootstrap.app_managed_authority import AppManagedAuthority
    binding, alice_token, _, account_open, catalog, entry = control
    # Bob is a real account, catalog member, and enrolled app-control subject.
    entry["accounts"] = ["alice", "bob"]
    catalog.write_text(json.dumps({"version": 1, "grants": [entry]}), encoding="utf8")
    with account_open() as connection:
        admin_auth.register(connection, "bob", "other-password", role="admin")
        bob_token, _ = admin_auth.login(connection, "bob", "other-password")
    root = Path(entry["roots"][0])
    db_path = str(tmp_path_factory.mktemp("qualified-learning") / "memory.db")
    monkeypatch.setenv("SONDER_DB", db_path)
    # The host returns its exact admitted absolute workspace scope. Compose
    # the real application with that exact opaque scope and its matching peer.
    config = _live_replication_config()
    config = replace(
        config,
        memory_replication=MemoryReplicationConfig(
            enabled=True,
            local_node_id="node-qualified",
            project_scope=str(root),
            peers=tuple(
                replace(peer, project_scope=str(root))
                for peer in config.memory_replication.peers
            ),
        ),
    )
    application = build_application(config=config)
    # PreparedWorkbenchAdapter obtains the host application from this entry
    # point.  Bind it to the same configured composition that owns learning,
    # so the dispatcher identity fence exercises the production path.
    monkeypatch.setattr(server, "_application", lambda: application)
    # Each managed command uses a separate real control/lane store. Completed
    # lanes deliberately retain an exclusive workspace reservation until an
    # attached host archives them; independent authenticated dispatchers do
    # not bypass that lifecycle just to reuse the same verified workspace.
    active = {}
    exits = iter((0, 0, 0, 7))  # alice, alice replayed evidence, bob, bob failure
    child_commands = iter(("child-1", "child-2", "child-3", "child-4"))
    current_verifier = [None]
    original_stage = _ManagedTurn.stage_final

    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    approval_gate = bridge(
        ApprovalLedger(root.parent / "qualification-approvals.db"),
        rule={"action": "allow", "pattern": "workspace_run"},
    )

    def approve(prepared, context):
        return approval_gate.authorize(
            "workspace_run", prepared.approval_payload(), surface="app-control",
            expires_at=time.time() + min(120, context.remaining_seconds),
        )

    def stage(view, facts):
        lanes = active["lanes"]
        with view._session._bound._scope() as current:
            child = lanes.spawn(
                command_id="qualification-" + next(child_commands),
                parent_session_id=view._session.parent_session_id,
                task="inspect",
                workspace_root=str(current.workspace_roots[0]),
                context=current,
                max_wall_seconds=600,
            )["lane"]
        lanes.run_pending(child["id"], current)
        verifier, gateway, proofs = _verifier(
            (lanes, lanes.store, lanes.gateway, current.workspace_roots[0], current, {}),
        )
        exit_code = next(exits)
        execute = gateway.execute_check

        def checked(*args, **kwargs):
            execute(*args, **kwargs)
            for proof in proofs.values():
                proof["status"] = "failed" if exit_code else "succeeded"
                proof["exit_code"] = exit_code
                proof["digest"] = hashlib.sha256(
                    json.dumps(
                        {key: value for key, value in proof.items() if key != "digest"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()

        gateway.execute_check = checked
        current_verifier[0] = verifier
        view._session._approve = approve
        verdict = view.verify_delegated(
            view._draft, verifier_factory=lambda *args: verifier,
        )
        assert verdict.valid is (exit_code == 0), verdict
        original_stage(
            view,
            replace(
                facts,
                delegated_work=True,
                validation_attempted=True,
                validation_passed=exit_code == 0,
                terminal_class="VALIDATION_FAILED" if exit_code else "NORMAL",
                certificate_id=verdict.certificate_id,
                certificate_generation=verdict.generation,
                certificate_code=verdict.code,
            ),
        )

    monkeypatch.setattr(_ManagedTurn, "stage_final", stage)
    recorder = ManagedLearningRecorder(
        application, verifier_factory=lambda *args: current_verifier[0],
    )

    def execute(principal, account_token, password, name):
        from sonder_runtime.adapters.security.control_plane_paths import (
            ControlPlanePaths,
            live_control_plane_inventory,
        )
        from sonder_runtime.bootstrap.app_control_http import AppControlBinding

        fleet_path = Path(binding.store.path).with_name(f"qualification-{name}.db")
        managed_binding = AppControlBinding(
            binding._config_provider,
            account_open=account_open,
            account_path=binding._account_path,
            fleet_path=lambda: fleet_path,
            private_inventory=lambda: live_control_plane_inventory(
                additional=lambda: ControlPlanePaths(
                    databases=(Path(binding._account_path()), fleet_path),
                    files=(catalog,),
                )
            ),
        )
        managed_binding.start()
        credential = invoke(
            managed_binding,
            account_token,
            "enroll",
            {
                "command_id": "enroll-" + name,
                "project": "project1",
                "password": password,
            },
        )[1]["control_token"]
        sessions = SQLiteSessionRepository(
            fleet_path.with_name(f"qualification-{name}-sessions.db")
        )
        lanes = AgentLaneService(
            SQLiteAgentLaneStore(managed_binding.store.path, sessions),
            sessions,
            type(
                "Gateway",
                (),
                {
                    "generate": lambda self, request, context: ModelResponse(
                        "Completed", "scripted", "code", tokens_out=1,
                    )
                },
            )(),
            auto_start=False,
            allowed_tools=("read_file",),
        )
        authority = AppManagedAuthority(managed_binding, lanes)
        selection = _selection_for(
            managed_binding,
            account_token=account_token,
            credential=credential,
            principal=principal,
            roots=(root,),
            command_id=f"{principal}-selection-{name}",
            expected_epoch=0,
        )
        active["lanes"] = lanes
        dispatcher = _dispatcher(authority, managed_binding, application, monkeypatch)
        dispatcher._eligibility = lambda lifetime, turn, finalized: lifetime.terminal_eligibility(
            turn, verifier_factory=lambda *args: current_verifier[0],
        )
        dispatcher._learning = recorder
        try:
            work = dispatcher.prepare(
                selection,
                command_id="qualification-" + name,
                request={
                    "prompt": "inspect repository", "tier": "code",
                    "allow_web": False, "max_steps": 1,
                },
            )
            dispatcher.execute(selection, work_id=work.prepared.work_id)
        finally:
            dispatcher.close()
        observer = authority.issue_selection(
            account_token=account_token,
            control_token=credential,
            context=OperationContext(
                "qualification-observe-" + name,
                "account:" + hashlib.sha256(principal.encode()).hexdigest(),
                "admin", "http", time.monotonic() + 300, Cancel(), (root,),
            ),
        )
        try:
            return dispatcher.status(observer, work_id=work.prepared.work_id)
        finally:
            authority.release_selection(observer)
            lanes.close()
            active.clear()

    try:
        first = execute("alice", alice_token, "test-password", "alice-first")
        repeated = execute("alice", alice_token, "test-password", "alice-repeat")
        assert first.state == repeated.state == "terminal", (first, repeated)
        first_rows = _rows(db_path)
        assert len(first_rows) == 2, recorder.recent()
        assert first_rows[0][1].content == first_rows[1][1].content
        assert first_rows[0][1].independent_key == first_rows[1][1].independent_key
        fact_id = "verified-subject-fact-" + first_rows[0][0].subject_digest
        with application.unit_of_work() as scope:
            assert not [
                row for row in scope.memory.facts_for_project(str(root))
                if row["id"] == fact_id
            ]

        execute("bob", bob_token, "other-password", "bob-second-principal")
        with application.unit_of_work() as scope:
            assert [row for row in scope.memory.facts_for_project(str(root)) if row["id"] == fact_id]

        failed = execute("bob", bob_token, "other-password", "bob-verified-failure")
        assert failed.state == "unknown" and failed.terminal is None
        rows = _rows(db_path)
        assert len(rows) == 4
        assert rows[-1][0].verifier_outcome == "failed"
        assert rows[-1][1].positive is False
        assert rows[-1][1].content == first_rows[0][1].content
        with application.unit_of_work() as scope:
            assert not [
                row for row in scope.memory.facts_for_project(str(root))
                if row["id"] == fact_id
            ]
        assert [outcome.promotion for outcome in recorder.recent()] == [
            "candidate", "candidate", "promoted", "demoted",
        ]
    finally:
        application.close_providers()

    reopened = build_application(config=config)
    try:
        with reopened.unit_of_work() as scope:
            assert not [
                row for row in scope.memory.facts_for_project(str(root))
                if row["id"] == fact_id
            ]
            assert len(SQLiteVerifierObservationRepository(scope.connection).list_pairs(limit=100)) == 4
    finally:
        reopened.close_providers()
