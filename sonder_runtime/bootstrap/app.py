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
import threading

from ..adapters.persistence.autopilot_repository import AutopilotRepository
from ..adapters.strangler_services import (
    LegacyPolicyRepository,
    LegacyProcessProbe,
    LegacyToolExecutor,
    LegacyUnitOfWork,
    OperationsEventSink,
)
from ..adapters.eval_history_reader import LegacyEvaluationHistoryReader
from ..adapters.inspection_executor import LegacyInspectionExecutor
from ..adapters.backup_gateway import LegacyBackupGateway
from ..adapters.recall_gateway import LegacyRecallGateway
from ..adapters.preference_adapters import (
    LegacyPreferenceCodec,
    LegacyPreferenceRepository,
    NullPreferenceEventSink,
)
from ..adapters.workflow_adapters import LegacyLoopRunner, LegacyWorkflowRepository
from ..adapters.local_observability import LocalObservabilitySink
from ..adapters.ollama.gateway import OllamaGateway
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


def _build_model_gateway() -> ModelGateway:
    """Select the model transport backend for this graph.

    Ollama is the default. ``SONDER_MODEL_BACKEND=openai`` (aliases:
    openai-compatible / llamacpp / vllm) selects the OpenAI-compatible gateway,
    which talks to any /v1 server; its own consent gate still refuses a
    non-loopback endpoint without cloud consent. Backend selection is a
    composition concern, so the env read lives here, not in a port or adapter.
    """
    import os

    backend = os.environ.get("SONDER_MODEL_BACKEND", "ollama").strip().lower()
    if backend in ("openai", "openai-compatible", "llamacpp", "vllm"):
        from ..adapters.openai_compat.gateway import OpenAICompatibleGateway

        return OpenAICompatibleGateway()
    return OllamaGateway()


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
    # SPEC-3 Phase 3: the real transport adapter behind the port — consent
    # enforced against the OperationContext, driver errors mapped into the
    # domain taxonomy. Backend is Ollama by default, selectable via env.
    gateway = _build_model_gateway()
    return Application(
        profile=profile,
        runtime_policy=RuntimePolicyService(LegacyPolicyRepository()),
        model_gateway=gateway,
        chat=ChatService(gateway),
        automation=AutopilotRepository(),
        # A UnitOfWork is per-transaction, so the graph exposes a factory, not
        # a singleton; each call opens and owns its own connection scope.
        unit_of_work=LegacyUnitOfWork,
        tool_executor=LegacyToolExecutor(),
        process_probe=LegacyProcessProbe(),
        # Bounded process-local counters/recent events decorate the durable
        # operations.db sink; they never replace its audit authority.
        events=LocalObservabilitySink(OperationsEventSink()),
        clock=SystemClock(),
        backup=BackupService(LegacyBackupGateway()),
        inspections=InspectionService(LegacyInspectionExecutor()),
        recall=RecallService(LegacyRecallGateway()),
        evaluation_history=EvaluationHistoryService(
            LegacyEvaluationHistoryReader()
        ),
        preferences=PreferenceService(
            LegacyPreferenceRepository(preference_connection_factory),
            LegacyPreferenceCodec(preference_module_provider),
            NullPreferenceEventSink(),
        ),
        workflows=WorkflowService(LegacyWorkflowRepository(), LegacyLoopRunner()),
    )


_default: Application | None = None
_default_lock = threading.Lock()


def default_app() -> Application:
    """Process-wide default graph for compatibility shims."""
    global _default
    with _default_lock:
        if _default is None:
            # Two first callers previously built different process-wide graphs,
            # splitting stateful adapters between requests during startup.
            _default = build_application()
        return _default


def reset_for_tests() -> None:
    global _default
    _default = None
