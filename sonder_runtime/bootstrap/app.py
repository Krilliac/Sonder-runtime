"""Deterministic composition root (SPEC-3 R-M8).

Runtime variants are assembled here — never through import-time global
initialization. Importing this module creates no directories, opens no
databases, reads no mutable environment state, starts no threads, probes
no hardware, and contacts no services; construction happens inside
``build_application`` and services stay lazy until first use.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..adapters.persistence.autopilot_repository import AutopilotRepository
from ..adapters.persistence.session_repository import SQLiteSessionRepository
from ..adapters.process_probe import ProcessProbeAdapter
from ..adapters.runtime_policy_repository import RuntimePolicyRepository
from ..adapters.tool_executor import ToolExecutorAdapter
from ..adapters.unit_of_work import UnitOfWorkAdapter
from ..adapters.operations_event_sink import OperationsEventSink
from ..adapters.evaluation_history_reader import EvaluationHistoryReaderAdapter
from ..adapters.inspection_executor import InspectionExecutorAdapter
from ..adapters.backup_gateway import LegacyBackupGateway
from ..adapters.recall_gateway import LegacyRecallGateway
from ..adapters.preference_adapters import (
    LegacyPreferenceRepository,
    NullPreferenceEventSink,
)
from ..adapters.preference_codec import PreferenceCodecAdapter
from ..adapters.workflow_repository import WorkflowRepositoryAdapter
from ..adapters.workflow_loop_runner import LoopRunnerAdapter
from ..adapters.local_observability import LocalObservabilitySink
from ..adapters.model_gateway_factory import build_model_gateway
from ..adapters.application_lifecycle import ApplicationLifecycle
from ..adapters.system_clock import SystemClock
from ..application.chat.handle_chat import ChatService
from ..application.backup import BackupService
from ..application.evaluation_history import EvaluationHistoryService
from ..application.inspection import InspectionService
from ..application.recall import RecallService
from ..application.preferences import PreferenceService
from ..application.ports.preferences import (
    ConnectionFactory,
    PreferenceModuleProvider,
)
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.ports.session_repository import SessionRepository
from ..application.ports.process_probe import ProcessProbe
from ..application.ports.repositories import AutomationRepository, UnitOfWork
from ..application.ports.tool_executor import ToolExecutor
from ..application.runtime_policy.use_cases import RuntimePolicyService
from ..application.workflows.use_cases import WorkflowService

PROFILES = ("workstation-local", "server-private")


@dataclass(frozen=True)
class Application:
    profile: str
    runtime_policy: RuntimePolicyService
    model_gateway: ModelGateway
    chat: ChatService
    automation: AutomationRepository
    unit_of_work: Callable[[], UnitOfWork]
    tool_executor: ToolExecutor
    process_probe: ProcessProbe
    events: EventSink
    clock: Clock
    backup: BackupService
    inspections: InspectionService
    recall: RecallService
    evaluation_history: EvaluationHistoryService
    preferences: PreferenceService
    workflows: WorkflowService
    session_repository: Callable[[], SessionRepository]


# Compatibility name for callers that used the bootstrap-private selector.
_build_model_gateway = build_model_gateway


def build_application(
    profile: str = "workstation-local",
    *,
    preference_connection_factory: ConnectionFactory | None = None,
    preference_module_provider: PreferenceModuleProvider | None = None,
) -> Application:
    """Assemble one application graph for the selected profile.

    Entry points call this exactly once. As SPEC-3 phases extract more
    bounded contexts, their services join this graph; until then the
    legacy adapters wrap the root modules.
    """
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected {PROFILES}")
    # SPEC-3 Phase 3: bind the transitional provider only at composition time.
    from .legacy_model import configure_legacy_model_providers
    configure_legacy_model_providers()
    # SPEC-3 Phase 3: the real transport adapter behind the port — consent
    # enforced against the OperationContext, driver errors mapped into the
    # domain taxonomy. Backend is Ollama by default, selectable via env.
    gateway = build_model_gateway()
    session_repository: SQLiteSessionRepository | None = None

    def get_session_repository() -> SessionRepository:
        nonlocal session_repository
        if session_repository is None:
            from ..platform.paths import state_path

            session_repository = SQLiteSessionRepository(
                state_path("sessions.db", "SONDER_SESSIONS_DB")
            )
        return session_repository

    return Application(
        profile=profile,
        runtime_policy=RuntimePolicyService(RuntimePolicyRepository()),
        model_gateway=gateway,
        chat=ChatService(gateway),
        automation=AutopilotRepository(),
        # A UnitOfWork is per-transaction, so the graph exposes a factory, not
        # a singleton; each call opens and owns its own connection scope.
        unit_of_work=UnitOfWorkAdapter,
        tool_executor=ToolExecutorAdapter(),
        process_probe=ProcessProbeAdapter(),
        # Bounded process-local counters/recent events decorate the durable
        # operations.db sink; they never replace its audit authority.
        events=LocalObservabilitySink(OperationsEventSink()),
        clock=SystemClock(),
        backup=BackupService(LegacyBackupGateway()),
        inspections=InspectionService(InspectionExecutorAdapter()),
        recall=RecallService(LegacyRecallGateway()),
        evaluation_history=EvaluationHistoryService(
            EvaluationHistoryReaderAdapter()
        ),
        preferences=PreferenceService(
            LegacyPreferenceRepository(preference_connection_factory),
            PreferenceCodecAdapter(preference_module_provider),
            NullPreferenceEventSink(),
        ),
        workflows=WorkflowService(WorkflowRepositoryAdapter(), LoopRunnerAdapter()),
        session_repository=get_session_repository,
    )


_application_lifecycle = ApplicationLifecycle(lambda: build_application())


def default_app() -> Application:
    """Process-wide default graph for compatibility shims."""
    return _application_lifecycle.get()


def reset_for_tests() -> None:
    _application_lifecycle.reset()
