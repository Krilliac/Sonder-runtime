"""Deterministic gateway call identities across a child crash and resume (#515).

A real child interpreter composes the application with ``build_application``
and dispatches a delegation through ``DelegationService``.  Its runner saves
a checkpoint and then appends to a workspace file through the production
typed tool gateway (``application.tools``), and the interpreter is killed
with ``os._exit`` at one cut.  A second composition (this test process)
repeats the exact delegation, so ``LocalSubagentProvider`` resumes the child
from its stamped checkpoint and the runner re-issues the gateway call.

Before this change the gateway keyed that call by a fresh ``request_id``;
the re-issued call was a new intent and the append ran twice.  Now:

* receipt settled, then crash: the re-issued call carries the same
  deterministic identity and is refused with ``SettledEffectReplay`` before
  any journal write; the runner consumes the receipt, the file holds one
  append, and a genuinely new call after it still runs at the next ordinal;
* append performed but no receipt, then crash: the intent is unresolved, the
  run stays fenced, the resume is refused and nothing runs again;
* receipt settled, then a resumed runner issuing a *different* request at
  that ordinal: ``DivergentEffectReplay``, the child fails recoverably and
  the file is unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

CRASH_EXIT = 93
CHILD = "wiring-gateway-child"
DELEGATION = "wiring-gateway-delegation"
_APP: dict = {}

pytestmark = pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")


def _config(root: Path):
    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def _trace(root: Path, line: str) -> None:
    with (root / "runner-trace.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _append(root: Path, content: str):
    """Append ``content`` through the composed typed tool gateway."""
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolPermission,
        ToolScope,
    )

    workspace = root / "workspace"
    return _APP["application"].tools.execute(ToolGatewayRequest(
        # A fresh id per call, exactly as a real caller supplies it.
        request_id="child-call-" + uuid.uuid4().hex,
        tool_name="write_file",
        arguments={"path": "append.txt", "content": content, "mode": "append"},
        scope=ToolScope(
            "wiring-child", (str(workspace),), frozenset({"write_files"}), source="worker",
        ),
        permission=ToolPermission(frozenset({"write_files"})),
    ))


def _runner_factory(root: Path, *, first: str, crash_after_first: bool, follow_up: str = ""):
    """Checkpoint, append ``first`` through the gateway, optionally append more."""
    from sonder_runtime.application.execution.effect_journal import (
        DivergentEffectReplay,
        SettledEffectReplay,
    )

    def bind(request, _context):
        def run(state, save, _control):
            if int(state.get("step", 0)) == 0:
                save({"step": 1}, "before-append")
            try:
                receipt = _append(root, first)
            except SettledEffectReplay as settled:
                _trace(root, f"consumed {first}")
                receipt_key = settled.receipt_key
            except DivergentEffectReplay:
                _trace(root, f"divergent {first}")
                raise
            else:
                assert receipt.success, receipt
                _trace(root, f"wrote {first}")
                if crash_after_first:
                    # The receipt committed; the next checkpoint was never saved.
                    os._exit(CRASH_EXIT)
                receipt_key = receipt.request_id
            if follow_up:
                assert _append(root, follow_up).success
                _trace(root, f"wrote {follow_up}")
            save({"step": 2, "first_receipt": receipt_key}, "after-append")
            return "gateway output"

        return run

    return lambda *_args: bind


def _checkpointed_calls_factory(root: Path, *, crash_after_second: bool):
    """Append ``a``, checkpoint, append ``b``: the second call follows a checkpoint."""
    from sonder_runtime.application.execution.effect_journal import SettledEffectReplay

    def bind(request, _context):
        def run(state, save, _control):
            if int(state.get("step", 0)) == 0:
                assert _append(root, "a").success
                _trace(root, "wrote a")
                save({"step": 1}, "after-a")
            try:
                receipt = _append(root, "b")
            except SettledEffectReplay:
                _trace(root, "consumed b")
            else:
                assert receipt.success, receipt
                _trace(root, "wrote b")
                if crash_after_second:
                    os._exit(CRASH_EXIT)
            save({"step": 2}, "after-b")
            return "checkpointed output"

        return run

    return lambda *_args: bind


def _delegate(application, root: Path):
    from sonder_runtime.application.agents.lineage_delegation import (
        DelegationRequest,
        LineageRecord,
        WorkspaceAssignment,
    )
    from sonder_runtime.application.agents.presets import resolve_preset
    from sonder_runtime.application.context import local_owner_context

    workspace = root / "workspace"
    delegation = application.delegation_service()
    context = local_owner_context(correlation_id="wiring-gateway-op", workspace_roots=(workspace,))
    root_id = delegation.root_id_for_context(context)
    preset = resolve_preset("researcher")
    assignment = WorkspaceAssignment((str(workspace),))
    lineage = LineageRecord(
        "wiring-gateway-lineage", root_id, root_id, CHILD, 1, preset.name, preset.role, assignment,
    )
    request = DelegationRequest(DELEGATION, lineage, "append through the gateway", preset, assignment)
    return delegation.dispatch(request, context)


def _prepare_process(root: Path) -> None:
    import permission_modes
    from sonder_runtime.adapters.filesystem import file_ops

    workspace = root / "workspace"
    file_ops.workspace_root = lambda: workspace
    # An unattended worker surface: ``auto`` answers the file-change class.
    permission_modes.set_mode(permission_modes.AUTO)


def _crashing_owner(root: Path, cut: str) -> None:
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.bootstrap.app import build_application

    _prepare_process(root)
    conversational_subagents.conversational_runner_factory = (
        _checkpointed_calls_factory(root, crash_after_second=True)
        if cut == "after_checkpointed_call"
        else _runner_factory(root, first="x", crash_after_first=cut == "after_receipt")
    )
    if cut == "in_flight":
        actual_write = file_ops.write_file

        def write_then_crash(*args, **kwargs):
            result = actual_write(*args, **kwargs)
            if (root / "workspace" / "append.txt").read_text(encoding="utf-8") != "x":
                os._exit(3)
            # The append is on disk; the gateway never publishes a receipt.
            _trace(root, "wrote x (no receipt)")
            os._exit(CRASH_EXIT)
            return result

        file_ops.write_file = write_then_crash
    application = build_application(config=_config(root))
    _APP["application"] = application
    _delegate(application, root).result(timeout=60)
    os._exit(4)  # The crash cut inside the runner must have fired.


def _crash(root: Path, cut: str) -> None:
    (root / "workspace").mkdir(parents=True)
    repo_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(filter(None, (str(repo_root), os.environ.get("PYTHONPATH")))),
    }
    crashed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--crash-owner", str(root), cut],
        cwd=repo_root, env=environment, capture_output=True, text=True,
        timeout=120, check=False,
    )
    assert crashed.returncode == CRASH_EXIT, (crashed.returncode, crashed.stderr[-3000:])


def _compose(root: Path, monkeypatch, factory=None, **runner):
    import permission_modes
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.bootstrap.app import build_application

    monkeypatch.setattr(file_ops, "workspace_root", lambda: root / "workspace")
    before = permission_modes.current_mode()
    permission_modes.set_mode(permission_modes.AUTO)
    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        factory or _runner_factory(root, **runner),
    )
    application = build_application(config=_config(root))
    _APP["application"] = application
    return application, lambda: permission_modes.set_mode(before)


def _journal(root: Path):
    from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
        SQLiteEffectJournal,
    )

    return SQLiteEffectJournal(root / "state" / "worker-effects.db")


def _records(root: Path):
    return _journal(root).effects_since(f"subagent:{CHILD}", 0).records


def _trace_lines(root: Path) -> list[str]:
    return (root / "runner-trace.log").read_text(encoding="utf-8").splitlines()


def _call_operation(ordinal: int) -> str:
    from sonder_runtime.application.execution.gateway_calls import (
        gateway_call_operation_id,
    )

    return gateway_call_operation_id(CHILD, 1, ordinal)


def test_settled_gateway_call_is_consumed_not_repeated_after_resume(tmp_path, monkeypatch):
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    _crash(tmp_path, "after_receipt")
    target = tmp_path / "workspace" / "append.txt"
    assert target.read_text(encoding="utf-8") == "x"
    crashed = _records(tmp_path)
    assert [(r.operation_id, r.state) for r in crashed] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        (_call_operation(1), EffectState.COMPLETED),
    ]
    settled_call = crashed[1]
    # Deterministic identity: not the fresh request id the caller supplied.
    assert settled_call.receipt_key.startswith("child-call-")
    assert settled_call.receipt_key not in settled_call.idempotency_key

    application, restore_mode = _compose(
        tmp_path, monkeypatch, first="x", crash_after_first=False, follow_up="y",
    )
    try:
        repository = application.delegation_service()._provider._local_service._repository
        stamped = repository.get(CHILD).checkpoint.provenance
        # The checkpoint was saved before the call: no ordinal issued yet.
        assert stamped is not None and stamped.digest_valid
        assert (stamped.version, stamped.gateway_call_ordinal) == (2, 0)

        result = _delegate(application, tmp_path).result(timeout=60)
        assert result.status is SubagentStatus.SUCCEEDED, result
        final = repository.get(CHILD)
    finally:
        application.close_delegation(timeout=10)
        restore_mode()

    # The settled append was consumed; the new call ran once at ordinal 2.
    assert target.read_text(encoding="utf-8") == "xy"
    assert _trace_lines(tmp_path) == ["wrote x", "consumed x", "wrote y"]
    after = _records(tmp_path)
    assert [(r.operation_id, r.state) for r in after] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        (_call_operation(1), EffectState.COMPLETED),
        (_call_operation(2), EffectState.COMPLETED),
    ]
    assert after[1] == settled_call
    assert final.checkpoint.state == {"step": 2, "first_receipt": settled_call.receipt_key}
    assert final.checkpoint.provenance.gateway_call_ordinal == 2


def test_resumed_runner_continues_the_call_ordinal_recorded_in_its_checkpoint(
    tmp_path, monkeypatch,
):
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    _crash(tmp_path, "after_checkpointed_call")
    target = tmp_path / "workspace" / "append.txt"
    assert target.read_text(encoding="utf-8") == "ab"
    crashed = _records(tmp_path)
    assert [(r.operation_id, r.state) for r in crashed] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        (_call_operation(1), EffectState.COMPLETED),
        (_call_operation(2), EffectState.COMPLETED),
    ]

    application, restore_mode = _compose(
        tmp_path, monkeypatch,
        factory=_checkpointed_calls_factory(tmp_path, crash_after_second=False),
    )
    try:
        repository = application.delegation_service()._provider._local_service._repository
        stamped = repository.get(CHILD).checkpoint.provenance
        # Saved after call 1: the host recorded one issued ordinal.
        assert repository.get(CHILD).checkpoint.state == {"step": 1}
        assert stamped.gateway_call_ordinal == 1 and stamped.digest_valid
        result = _delegate(application, tmp_path).result(timeout=60)
        assert result.status is SubagentStatus.SUCCEEDED, result
        final = repository.get(CHILD)
    finally:
        application.close_delegation(timeout=10)
        restore_mode()

    # Resumed from the checkpoint at ordinal 1: ``b`` is re-issued at
    # ordinal 2, meets its settled receipt and is not appended again.
    assert target.read_text(encoding="utf-8") == "ab"
    assert _trace_lines(tmp_path) == ["wrote a", "wrote b", "consumed b"]
    assert _records(tmp_path) == crashed
    assert final.checkpoint.provenance.gateway_call_ordinal == 2


def test_in_flight_gateway_call_stays_fenced_after_crash(tmp_path, monkeypatch):
    from sonder_runtime.application.execution.effect_journal import (
        EffectJournalError,
        EffectState,
    )
    from sonder_runtime.application.ports.subagents import SubagentStatus

    _crash(tmp_path, "in_flight")
    target = tmp_path / "workspace" / "append.txt"
    assert target.read_text(encoding="utf-8") == "x"
    assert [(r.operation_id, r.state) for r in _records(tmp_path)] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        (_call_operation(1), EffectState.INTENT),
    ]

    application, restore_mode = _compose(
        tmp_path, monkeypatch, first="x", crash_after_first=False,
    )
    try:
        # Startup reconciliation has no verifier for gateway calls: the
        # intent is uncertain and the child's run stays fenced.
        assert _records(tmp_path)[1].state is EffectState.UNCERTAIN
        with pytest.raises(EffectJournalError):
            _delegate(application, tmp_path)
        repository = application.delegation_service()._provider._local_service._repository
        child = repository.get(CHILD)
        assert child.status is not SubagentStatus.SUCCEEDED
    finally:
        application.close_delegation(timeout=10)
        restore_mode()

    assert target.read_text(encoding="utf-8") == "x"
    assert _trace_lines(tmp_path) == ["wrote x (no receipt)"]
    records = _records(tmp_path)
    assert [(r.operation_id, r.state) for r in records] == [
        (f"subagent-dispatch:{CHILD}", EffectState.COMPLETED),
        (_call_operation(1), EffectState.UNCERTAIN),
    ]


def test_divergent_gateway_call_at_the_same_ordinal_is_refused(tmp_path, monkeypatch):
    from sonder_runtime.application.execution.effect_journal import EffectState
    from sonder_runtime.application.ports.subagents import SubagentStatus

    _crash(tmp_path, "after_receipt")
    target = tmp_path / "workspace" / "append.txt"
    settled = _records(tmp_path)

    # The resumed runner asks for a different append at ordinal 1.
    application, restore_mode = _compose(
        tmp_path, monkeypatch, first="z", crash_after_first=False,
    )
    try:
        result = _delegate(application, tmp_path).result(timeout=60)
        assert result.status is SubagentStatus.FAILED, result
        assert result.error is not None and result.error.code == "runner_failed"
        assert "divergent replay" in result.error.message
        repository = application.delegation_service()._provider._local_service._repository
        child = repository.get(CHILD)
        assert child.recovery_required
        assert child.checkpoint.state == {"step": 1}
    finally:
        application.close_delegation(timeout=10)
        restore_mode()

    assert target.read_text(encoding="utf-8") == "x"
    assert _trace_lines(tmp_path) == ["wrote x", "divergent z"]
    after = _records(tmp_path)
    assert after == settled
    assert after[1].state is EffectState.COMPLETED


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--crash-owner":
    _crashing_owner(Path(sys.argv[2]), sys.argv[3])
