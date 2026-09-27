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
