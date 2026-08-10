"""Strangler adapters: root-module implementations behind SPEC-3 ports.

The strangler strategy (SPEC-3 section 11) wraps the existing flat
modules as adapters first, then moves implementations behind the ports
use case by use case. Imports are deliberately lazy — building the
application graph must not drag in the 500KB server module until a
service is actually exercised.
"""
from __future__ import annotations

import time

from ..application.context import OperationContext
from ..application.ports.model_gateway import (
    Embedding,
    ModelRequest,
    ModelResponse,
    require_embedding_vector,
    require_model_text,
)
from ..application.ports.process_probe import ProbeResult, ProcessIdentity
from ..application.ports.tool_executor import ToolCall, ToolResult
from ..domain.common.errors import Cancelled, DeadlineExceeded, InvalidInput


def _check_context_liveness(context: OperationContext) -> None:
    if context.expired:
        raise DeadlineExceeded("operation deadline exceeded before adapter call")
    if context.cancellation is not None and context.cancellation.cancelled:
        raise Cancelled("operation cancelled before adapter call")


class LegacyPolicyRepository:
    """PolicyRepository over the root runtime_policy module."""

    def load(self) -> dict:
        import runtime_policy

        return runtime_policy.load()

    def update(
        self,
        *,
        local_models: dict | None = None,
        routing: dict | None = None,
        npu: dict | None = None,
        expected_revision: int | None = None,
        source: str = "application",
    ) -> dict:
        import runtime_policy

        return runtime_policy.update(
            local_models=local_models,
            routing=routing,
            npu=npu,
            source=source,
            expected_revision=expected_revision,
        )


class LegacyAutomationRepository:
    """AutomationRepository over the root ``autopilot_store`` module.

    The store owns its own SQLite connections and lease bookkeeping, so this
    adapter holds no state and every call is a lazy-imported delegation —
    building it drags in nothing and opens no database. ``lease_seconds=None``
    means "use the store's own default lease".
    """

    def create_run(
        self,
        objective: str,
        *,
        project: str = "",
        tier: str = "code",
        policy: str = "workspace",
        allow_web: bool = True,
        max_failures: int = 3,
        max_tasks: int = 12,
        max_replans: int = 2,
        adaptive: bool = True,
    ) -> dict:
        import autopilot_store

        return autopilot_store.create_run(
            objective,
            project=project,
            tier=tier,
            policy=policy,
            allow_web=allow_web,
            max_failures=max_failures,
            max_tasks=max_tasks,
            max_replans=max_replans,
            adaptive=adaptive,
        )

    def get_run(self, selector: str = "") -> dict | None:
        import autopilot_store

        return autopilot_store.get_run(selector)

    def list_runs(
        self, include_finished: bool = True, limit: int = 20
    ) -> list:
        import autopilot_store

        return autopilot_store.list_runs(
            include_finished=include_finished, limit=limit
        )

    def claim_run(
        self,
        selector: str,
        owner_id: str,
        *,
        owner_pid: int,
        lease_seconds: int | None = None,
    ) -> dict | None:
        import autopilot_store

        if lease_seconds is None:
            return autopilot_store.claim_run(selector, owner_id, owner_pid=owner_pid)
        return autopilot_store.claim_run(
            selector, owner_id, owner_pid=owner_pid, lease_seconds=lease_seconds
        )

    def save_progress(self, run_id: str, owner_id: str, **changes) -> dict | None:
        import autopilot_store

        return autopilot_store.save_progress(run_id, owner_id, **changes)

    def heartbeat(
        self, run_id: str, owner_id: str, lease_seconds: int | None = None
    ) -> bool:
        import autopilot_store

        if lease_seconds is None:
            return autopilot_store.heartbeat(run_id, owner_id)
        return autopilot_store.heartbeat(run_id, owner_id, lease_seconds)

    def request_pause(self, selector: str) -> dict | None:
        import autopilot_store

        return autopilot_store.request_pause(selector)

    def request_cancel(self, selector: str) -> dict | None:
        import autopilot_store

        return autopilot_store.request_cancel(selector)

    def control_flags(self, run_id: str, owner_id: str) -> dict:
        import autopilot_store

        return autopilot_store.control_flags(run_id, owner_id)

    def finish_run(
        self,
        run_id: str,
        owner_id: str,
        status: str,
        *,
        summary: str = "",
        final_report: str = "",
        last_error: str = "",
    ) -> dict | None:
        import autopilot_store

        return autopilot_store.finish_run(
            run_id,
            owner_id,
            status,
            summary=summary,
            final_report=final_report,
            last_error=last_error,
        )

    def reconcile_stale_runs(self, now: float | None = None) -> int:
        import autopilot_store

        return autopilot_store.reconcile_stale_runs(now)

    def events(self, selector: str = "", limit: int = 20) -> list:
        import autopilot_store

        return autopilot_store.events(selector, limit=limit)

    def snapshot(self, include_finished: bool = True, limit: int = 20) -> dict:
        import autopilot_store

        return autopilot_store.snapshot(
            include_finished=include_finished, limit=limit
        )


class LegacyMemoryRepository:
    """MemoryRepository over the migrated memory and recall adapters.

    Bound to a live SQLite connection owned by the UnitOfWork — all methods
    delegate against that one connection. Constructed by the UnitOfWork, not
    standalone.
    """

    def __init__(self, conn) -> None:
        self._conn = conn

    def add_fact(self, fact_id: str, project: str, text: str, embedding=None) -> None:
        import sonder_runtime.adapters.memory_store as memory_store

        memory_store.add_fact(self._conn, fact_id, project, text, embedding)

    def facts_for_project(self, project: str) -> list:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.facts_for_project(self._conn, project)

    def count_facts(self, project: str) -> int:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.count_facts(self._conn, project)

    def log_interaction(
        self,
        interaction_id: str,
        task: str,
        retrieved_ctx,
        response: str,
        tier: str,
        **fields,
    ) -> None:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.log_interaction(
            self._conn, interaction_id, task, retrieved_ctx, response, tier, **fields
        )

    def get_interaction(self, interaction_id: str) -> dict | None:
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.get_interaction(self._conn, interaction_id)

    def recall(self, task: str, *, k: int = 2, project: str | None = None, **options):
        import sonder_runtime.adapters.recall as recall_module

        return recall_module.recall(self._conn, task, k=k, project=project, **options)

    def record_outcome(
        self, interaction_id: str, signal: str, reward_value: float, **options
    ):
        import sonder_runtime.adapters.memory_store as memory_store

        return memory_store.record_outcome_and_claim_lesson_distillation(
            self._conn, interaction_id, signal, reward_value, **options
        )


class LegacyUnitOfWork:
    """UnitOfWork owning one memory-store connection for a transaction scope.

    Opens the canonical memory database on ``__enter__`` and exposes the
    memory repository bound to that connection; the automation and policy
    repositories are connection-independent (they manage their own stores),
    and events go to the SPEC-2 operations store.

    Honest boundary note: several root ``memory_store`` operations (e.g.
    ``add_fact``, ``log_interaction``) still self-commit, so ``rollback`` does
    not yet undo them. The UnitOfWork owns the connection lifecycle today; the
    transaction boundary tightens as those ops migrate off self-commit.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn = None
        self.memory = None
        self.automation = LegacyAutomationRepository()
        self.policy = LegacyPolicyRepository()
        self.events = OperationsEventSink()

    def __enter__(self) -> "LegacyUnitOfWork":
        import sonder_runtime.adapters.memory_store as memory_store
        import sonder_paths

        path = self._db_path or sonder_paths.memory_db_path()
        self._conn = memory_store.connect(path)
        self.memory = LegacyMemoryRepository(self._conn)
        return self

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self.memory = None


class LegacyModelGateway:
    """ModelGateway over the root server module's chat entry point."""

    def generate(
        self, request: ModelRequest, context: OperationContext
    ) -> ModelResponse:
        import server

        if not (request.prompt or "").strip():
            raise InvalidInput("model request prompt is empty")
        _check_context_liveness(context)
        started = time.monotonic()
        text = server.sonder(
            request.prompt,
            history=list(request.history) or None,
            tier=request.tier or None,
        )
        return ModelResponse(
            text=require_model_text(text),
            model=request.tier or "sonder",
            tier=request.tier or "general",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def embed(self, texts, context: OperationContext):
        import embeddings

        results = []
        for text in texts:
            _check_context_liveness(context)
            results.append(Embedding(
                vector=require_embedding_vector(embeddings.embed(text)),
                model="local",
            ))
        return results


class OperationsEventSink:
    """EventSink over the SPEC-2 operations store."""

    def __init__(self) -> None:
        self._store = None

    def emit(
        self,
        event_code: str,
        *,
        summary: str,
        detail: dict | None = None,
        severity: str = "INFO",
        correlation_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        try:
            if self._store is None:
                from sonder_operations_store import OperationsStore

                self._store = OperationsStore()
            self._store.record_event(
                component="application",
                event_code=event_code,
                severity=severity,
                summary=summary,
                detail=detail,
                correlation_id=correlation_id,
                operation_id=operation_id,
            )
        except Exception:
            return


class SystemClock:
    def now_utc_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def monotonic(self) -> float:
        return time.monotonic()


class LegacyProcessProbe:
    """ProcessProbe over the adapter ``process_liveness`` module.

    ``process_liveness`` encodes start-time / boot-id into one opaque identity
    string rather than a separate float, so ``started_at`` stays 0.0 and the
    fingerprint carries the real instance identity. Unknown liveness maps to
    ``ProbeResult.UNKNOWN`` so it can never drive a split-brain ownership claim.
    """

    def identity(self, pid: int) -> ProcessIdentity | None:
        import sonder_runtime.adapters.process_liveness as process_liveness

        state, fingerprint = process_liveness.probe_process(pid)
        if state == process_liveness.PROCESS_DEAD or not fingerprint:
            return None
        return ProcessIdentity(
            pid=int(pid), started_at=0.0, fingerprint=str(fingerprint)
        )

    def is_same_live_process(self, identity: ProcessIdentity) -> ProbeResult:
        import sonder_runtime.adapters.process_liveness as process_liveness

        state, _observed = process_liveness.probe_process(
            identity.pid, expected_identity=identity.fingerprint or None
        )
        if state == process_liveness.PROCESS_ALIVE:
            return ProbeResult.ALIVE
        if state == process_liveness.PROCESS_DEAD:
            return ProbeResult.DEAD
        return ProbeResult.UNKNOWN


class LegacyToolExecutor:
    """ToolExecutor over the guarded ``workbench`` / ``file_ops`` primitives.

    Covers the core mutating/execution primitives (program/script execution and
    guarded file operations); broader tool coverage arrives as server call-sites
    migrate behind this port. The ``OperationContext`` is accepted for the seam;
    ``workbench``/``file_ops`` still enforce their own containment/auth guards,
    and a guard rejection surfaces as ``ok=False`` rather than an exception.
    """

    def execute(self, call: ToolCall, context: OperationContext) -> ToolResult:
        args = dict(call.arguments or {})
        if context.expired:
            return ToolResult(ok=False, error_code="DeadlineExceeded",
                              output="operation deadline exceeded")
        if context.cancellation is not None and context.cancellation.cancelled:
            return ToolResult(ok=False, error_code="Cancelled",
                              output="operation cancelled")
        try:
            if call.tool == "run_program":
                import workbench

                res = workbench.run_program(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=str(res.get("stdout", "")),
                    evidence=res,
                )
            if call.tool == "run_script":
                import workbench

                res = workbench.run_script(**args)
                return ToolResult(
                    ok=bool(res.get("ok")),
                    output=str(res.get("stdout", "")),
                    evidence=res,
                )
            if call.tool == "read_file":
                import file_ops

                res = file_ops.read_file(**args)
                return ToolResult(ok=True, output=str(res.get("text", "")), evidence=res)
            if call.tool == "write_file":
                import file_ops

                res = file_ops.write_file(**args)
                return ToolResult(
                    ok=True, evidence=res if isinstance(res, dict) else {"result": res}
                )
            if call.tool == "edit_file":
                import file_ops

                res = file_ops.edit_file(**args)
                return ToolResult(
                    ok=True, evidence=res if isinstance(res, dict) else {"result": res}
                )
            if call.tool == "make_directory":
                import file_ops

                res = file_ops.make_directory(**args)
                return ToolResult(ok=True, evidence=res)
            return ToolResult(
                ok=False,
                error_code="unknown_tool",
                output="unsupported tool: %s" % call.tool,
            )
        except (PermissionError, ValueError, OSError, KeyError, TypeError) as exc:
            return ToolResult(
                ok=False, error_code=type(exc).__name__, output=str(exc)
            )
