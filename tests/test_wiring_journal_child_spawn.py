"""Concurrent-spawn decision for journaled child dispatch through composition (#515).

Operator decision: two identical concurrent spawns of the same child are an
idempotent join; a different request for the same child identity is refused;
a dispatch refused synchronously before any durable admission records a
definitive no-effect outcome and does not brick the child id for a corrected
dispatch.  Every case runs through ``build_application`` and the composed
``DelegationService`` / ``LocalSubagentProvider`` over the production
worker-effects journal.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

CHILD = "wiring-spawn-child"


def _config(root: Path):
    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def _application(root: Path, monkeypatch, runner):
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.bootstrap.app import build_application

    (root / "workspace").mkdir(exist_ok=True)
    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        lambda *_: lambda request, context: runner,
    )
    return build_application(config=_config(root))


def _delegation_request(delegation, root: Path, prompt: str = "inspect"):
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context

    workspace = root / "workspace"
    context = local_owner_context(correlation_id="wiring-spawn-op", workspace_roots=(workspace,))
    root_id = delegation.root_id_for_context(context)
    preset = resolve_preset("researcher")
    assignment = WorkspaceAssignment((str(workspace),))
    lineage = LineageRecord(
        "wiring-spawn-lineage", root_id, root_id, CHILD, 1, preset.name, preset.role, assignment,
    )
    return DelegationRequest("wiring-spawn-delegation", lineage, prompt, preset, assignment), context


def _run_records(root: Path, child_id: str):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal

    journal = SQLiteEffectJournal(root / "state" / "worker-effects.db")
    return journal.effects_since(f"subagent:{child_id}", 0).records


def test_identical_concurrent_spawns_join_one_runner(tmp_path, monkeypatch):
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    release = threading.Event()
    calls: list[str] = []

    def runner(state, save, control):
        calls.append("run")
        assert release.wait(30)
        return "joined output"

    application = _application(tmp_path, monkeypatch, runner)
    try:
        delegation = application.delegation_service()
        request, context = _delegation_request(delegation, tmp_path)
        start = threading.Barrier(2)
        handles: list[object] = []
        errors: list[BaseException] = []

        def dispatch():
            try:
                start.wait(10)
                handles.append(delegation.dispatch(request, context))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=dispatch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert errors == []
        assert [handle.child_id for handle in handles] == [CHILD, CHILD]
        release.set()
        results = [handle.result(timeout=30) for handle in handles]
        assert {result.status for result in results} == {SubagentStatus.SUCCEEDED}
        assert {result.output for result in results} == {"joined output"}
        assert calls == ["run"]

        # A later identical dispatch reuses the settled admission too.
        again = delegation.dispatch(request, context).result(timeout=30)
        assert again.output == "joined output" and calls == ["run"]
    finally:
        release.set()
        application.close_delegation(timeout=10)

    records = _run_records(tmp_path, CHILD)
    assert [(r.operation_id, r.state) for r in records] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
    ]


def test_same_child_with_different_request_digest_is_refused(tmp_path, monkeypatch):
    from sonder_runtime.application.ports.subagents import InvalidSubagentRequest, SubagentStatus

    application = _application(tmp_path, monkeypatch, lambda state, save, control: "first output")
    try:
        delegation = application.delegation_service()
        request, context = _delegation_request(delegation, tmp_path)
        first = delegation.dispatch(request, context).result(timeout=30)
        assert first.status is SubagentStatus.SUCCEEDED
        before = _run_records(tmp_path, CHILD)

        provider = delegation._provider
        stored = provider._local_service.record(CHILD).request
        with pytest.raises(InvalidSubagentRequest, match="different request"):
            provider.spawn(replace(stored, prompt="a different task"), context)
        # The public dispatch path refuses the conflicting delegation as well.
        conflicting, _ = _delegation_request(delegation, tmp_path, prompt="a different task")
        with pytest.raises(Exception):
            delegation.dispatch(conflicting, context)
    finally:
        application.close_delegation(timeout=10)
    assert _run_records(tmp_path, CHILD) == before


def test_refused_dispatch_does_not_brick_a_corrected_dispatch(tmp_path, monkeypatch):
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import (
        InvalidSubagentRequest,
        SubagentBudget,
        SubagentRequest,
        SubagentStatus,
    )

    calls: list[str] = []
    application = _application(
        tmp_path, monkeypatch, lambda state, save, control: calls.append("run") or "corrected",
    )
    child = "wiring-corrected-child"
    try:
        provider = application.delegation_service()._provider
        context = local_owner_context(
            correlation_id="wiring-corrected", workspace_roots=(tmp_path / "workspace",),
        )
        provider.register_root(
            "wiring-root", SubagentBudget(max_children=2, max_steps=4, max_wall_seconds=60),
            owner_id=context.principal_id,
        )
        too_wide = SubagentRequest(
            "wiring-root", "bounded", SubagentBudget(max_children=1, max_steps=40, max_wall_seconds=30),
            child, (), "corrected-task", "corrected-task",
        )
        with pytest.raises(InvalidSubagentRequest, match="widens parent max_steps"):
            provider.spawn(too_wide, context)
        refused = _run_records(tmp_path, child)
        assert [(r.operation_id, r.state) for r in refused] == [
            (f"subagent-dispatch:{child}", EffectState.FAILED),
        ]
        assert refused[0].receipt_key == f"subagent-dispatch-refused:{child}"

        corrected = replace(too_wide, budget=SubagentBudget(max_children=1, max_steps=4, max_wall_seconds=30))
        result = provider.spawn(corrected, context).result(timeout=30)
        assert result.status is SubagentStatus.SUCCEEDED and calls == ["run"]
    finally:
        application.close_delegation(timeout=10)
    records = _run_records(tmp_path, child)
    assert [(r.operation_id, r.state) for r in records] == [
        (f"subagent-dispatch:{child}", EffectState.FAILED),
        (f"subagent-dispatch:{child}#dispatch-attempt-2", EffectState.COMPLETED),
    ]
    assert records[1].idempotency_key == "corrected-task#dispatch-attempt-2"


def test_concurrent_spawn_never_reads_a_peers_unfinished_create_as_ambiguous(tmp_path, monkeypatch):
    """The losing admission of an identical concurrent spawn retains its create
    intent in one storage transaction and its receipt in a second one.  The
    winner must never judge that healthy, in-flight write as an ambiguous
    commit.  The interleaving is forced with events only: the loser is parked
    between its intent and receipt transactions until the winner's settlement
    check has run (unserialised code) or the winner is observed blocked on the
    service mutation lock (a serialising implementation)."""
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.worker_registry import continuation as reg_mod

    wait_s = 10
    calls: list[str] = []
    application = _application(tmp_path, monkeypatch, lambda state, save, control: calls.append("run") or "joined")
    try:
        delegation = application.delegation_service()
        service = delegation._provider._local_service
        repo = service._repository
        repo_cls = type(repo)
        for owner, name in ((reg_mod, "_ADMISSION_LOCK"), (service, "_lock"),
                            (service, "_require_storage_settled"),
                            (repo_cls, "mutate"), (repo_cls, "_connect"), (repo, "unresolved_mutation")):
            assert hasattr(owner, name), f"hook target {name!r} is missing on {owner!r}; update this test's hooks"
        request, context = _delegation_request(delegation, tmp_path)

        meta = threading.Lock()
        roles: dict[str, int] = {}             # "winner"/"loser" -> thread ident
        loser_parked = threading.Event()       # loser retained its intent, receipt not yet written
        winner_progress = threading.Event()    # winner's check ran, or winner is blocked on the service lock
        problems: list[str] = []
        local = threading.local()
        at_lock = threading.Barrier(2)

        def is_winner() -> bool:
            return roles.get("winner") == threading.get_ident()

        real_admission = reg_mod._ADMISSION_LOCK

        class AdmissionGate:
            def __enter__(self):
                try:
                    at_lock.wait(wait_s)
                except threading.BrokenBarrierError:
                    problems.append("both spawns did not reach the registry admission lock")
                real_admission.acquire()
                with meta:
                    role = "loser" if "winner" in roles else "winner"
                    roles[role] = threading.get_ident()
                return self

            def __exit__(self, *exc):
                real_admission.release()
                if is_winner() and not loser_parked.wait(wait_s):
                    problems.append("loser never parked between its create intent and receipt")
                return False

        real_lock = service._lock

        class ObservedServiceLock:
            def __enter__(self):
                if real_lock.acquire(blocking=False):
                    return self
                if loser_parked.is_set() and is_winner():
                    winner_progress.set()
                real_lock.acquire()
                return self

            def __exit__(self, *exc):
                real_lock.release()
                return False

        orig_settled = service._require_storage_settled

        def settled(child_id, *args, **kwargs):
            try:
                return orig_settled(child_id, *args, **kwargs)
            finally:
                if child_id == CHILD and is_winner() and loser_parked.is_set():
                    winner_progress.set()

        orig_mutate, orig_connect = repo_cls.mutate, repo_cls._connect

        def mutate(self, prepared):
            local.parking = (
                prepared.kind == "create" and prepared.child_id == CHILD
                and roles.get("loser") == threading.get_ident()
            )
            local.connects = 0
            try:
                return orig_mutate(self, prepared)
            finally:
                local.parking = False

        def connect(self):
            if getattr(local, "parking", False):
                local.connects += 1
                if local.connects == 2:  # intent transaction committed; receipt not yet begun
                    loser_parked.set()
                    if not winner_progress.wait(wait_s):
                        problems.append("winner made no progress while the loser was parked")
            return orig_connect(self)

        monkeypatch.setattr(reg_mod, "_ADMISSION_LOCK", AdmissionGate())
        monkeypatch.setattr(service, "_lock", ObservedServiceLock())
        monkeypatch.setattr(service, "_require_storage_settled", settled)
        monkeypatch.setattr(repo_cls, "mutate", mutate)
        monkeypatch.setattr(repo_cls, "_connect", connect)

        start = threading.Barrier(2)
        handles: list[object] = []
        errors: list[BaseException] = []

        def dispatch():
            try:
                start.wait(wait_s)
                handles.append(delegation.dispatch(request, context))
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=dispatch, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4 * wait_s)
        assert not any(thread.is_alive() for thread in threads), "a dispatch thread hung"
        assert problems == [], f"forced interleaving did not happen: {problems}"
        assert loser_parked.is_set()
        assert errors == []
        assert [handle.child_id for handle in handles] == [CHILD, CHILD]
        results = [handle.result(timeout=30) for handle in handles]
        assert {result.output for result in results} == {"joined"}
        assert calls == ["run"]
        assert repo.unresolved_mutation(CHILD) is None
    finally:
        application.close_delegation(timeout=10)

    records = _run_records(tmp_path, CHILD)
    assert [(r.operation_id, r.state) for r in records] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
    ]
