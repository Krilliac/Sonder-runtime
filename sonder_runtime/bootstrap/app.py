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
from ..adapters.persistence.fleet_registry import FleetStoreRegistryAdapter
from ..adapters.persistence.session_repository import SQLiteSessionRepository
from ..adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from ..adapters.persistence.sqlite.workflow_checkpoints import SQLiteWorkflowCheckpointRepository
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
from ..adapters.web_provider import LegacyWebProvider
from ..adapters.inference.ollama_vision import OllamaVisionGateway
from ..adapters.vision_input import FileVisionInputProvider
from ..adapters.application_lifecycle import ApplicationLifecycle
from ..adapters.extensions.host import ExtensionHost
from ..adapters.system_clock import SystemClock
from ..application.chat.handle_chat import ChatService
from ..application.vision import VisionService
from ..application.session import SessionCaptureService
from ..application.compaction import SessionCompactionService
from ..application.extensions.experiments import (
    EphemeralExperimentManager,
    StartupAuthority,
)
from ..application.extensions.registry import ExtensionRegistry
from ..application.backup import BackupService
from ..application.capabilities.jobs import JobRegistryService, ResumableWorkflowEngine
from ..application.jobs.session_lifecycle import JobRegistryLifecycleAdapter, JobSessionLifecycleRecorder
from ..application.agent_registry.unified import UnifiedAgentRegistryService
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
from ..application.ports.web import WebProvider
from ..application.ports.session_repository import SessionRepository
from ..application.ports.jobs import JobRegistry
from ..application.ports.process_probe import ProcessProbe
from ..application.ports.repositories import AutomationRepository, UnitOfWork
from ..application.ports.tool_executor import ToolExecutor
from ..application.runtime_policy.use_cases import RuntimePolicyService
from ..application.workflows.use_cases import WorkflowService
from ..platform.config import SonderConfig
from ..platform import paths as runtime_paths
from ..adapters.inference import ollama_endpoint

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
    session_capture_service: Callable[[], SessionCaptureService]
    job_registry: Callable[[], JobRegistry]
    job_service: Callable[[], JobRegistryService]
    config: SonderConfig | None = None
    vision: VisionService | None = None
    web_provider: WebProvider | None = None
    workflow_engine: Callable[[], ResumableWorkflowEngine] | None = None
    agent_registry: Callable[[], UnifiedAgentRegistryService] | None = None
    compaction_service: Callable[[], SessionCompactionService] | None = None
    extension_registry: Callable[[], ExtensionRegistry] | None = None
    experiment_manager: Callable[[], EphemeralExperimentManager] | None = None


# Compatibility name for callers that used the bootstrap-private selector.
_build_model_gateway = build_model_gateway


def build_application(
    profile: str = "workstation-local",
    *,
    config: SonderConfig | None = None,
    preference_connection_factory: ConnectionFactory | None = None,
    preference_module_provider: PreferenceModuleProvider | None = None,
    session_capture_service: SessionCaptureService | None = None,
    extension_startup_authority: StartupAuthority | None = None,
) -> Application:
    """Assemble one application graph for the selected profile.

    Entry points call this exactly once. As SPEC-3 phases extract more
    bounded contexts, their services join this graph; until then the
    legacy adapters wrap the root modules.
    """
    if config is not None:
        if not isinstance(config, SonderConfig):
            raise TypeError("config must be a SonderConfig when provided")
        profile = config.profile
        if config.state.home:
            # Keep typed startup state process-local.  This must happen before
            # any lazy persistence factory can resolve a database path, and
            # must not be translated through the mutable process environment.
            runtime_paths.configure_home(config.state.home)
        ollama_endpoint.configure_typed_endpoint(config.ollama.url)
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected {PROFILES}")
    from ..adapters.web import lifecycle as runtime_lifecycle
    runtime_lifecycle.configure(config)
    # Keep the transitional provider behind lazy closures: composing the
    # application must not import the historical root module.
    from .legacy_model import lazy_legacy_model_provider_factories
    target_resolver, generate_factory = lazy_legacy_model_provider_factories()
    # SPEC-3 Phase 3: the real transport adapter behind the port — consent
    # enforced against the OperationContext, driver errors mapped into the
    # domain taxonomy. Backend is Ollama by default, selectable via env.
    gateway = build_model_gateway(
        target_resolver=target_resolver,
        generate_factory=generate_factory,
    )
    vision = VisionService(
        FileVisionInputProvider(),
        OllamaVisionGateway(target_resolver=target_resolver),
    )
    session_repository: SQLiteSessionRepository | None = None
    canonical_session_capture: SessionCaptureService | None = session_capture_service
    compaction_service: SessionCompactionService | None = None
    job_registry: SQLiteDurableJobRegistry | None = None
    job_service: JobRegistryService | None = None
    workflow_engine: ResumableWorkflowEngine | None = None
    agent_registry: UnifiedAgentRegistryService | None = None
    extension_registry: ExtensionRegistry | None = None
    experiment_manager: EphemeralExperimentManager | None = None

    def get_session_repository() -> SessionRepository:
        nonlocal session_repository
        if session_repository is None:
            from ..platform.paths import state_path

            session_repository = SQLiteSessionRepository(
                state_path("sessions.db", "SONDER_SESSIONS_DB")
            )
        return session_repository

    def get_job_registry() -> JobRegistry:
        nonlocal job_registry
        if job_registry is None:
            from ..platform.paths import state_path

            job_registry = SQLiteDurableJobRegistry(
                state_path("jobs.db", "SONDER_JOBS_DB")
            )
        return job_registry

    def get_session_capture_service() -> SessionCaptureService:
        nonlocal canonical_session_capture
        if canonical_session_capture is None:
            canonical_session_capture = SessionCaptureService(get_session_repository())
        return canonical_session_capture

    def get_compaction_service() -> SessionCompactionService:
        nonlocal compaction_service
        if compaction_service is None:
            compaction_service = SessionCompactionService(get_session_repository())
        return compaction_service

    def get_job_service() -> JobRegistryService:
        nonlocal job_service
        if job_service is None:
            lifecycle = JobRegistryLifecycleAdapter(
                JobSessionLifecycleRecorder(get_session_repository())
            )
            job_service = JobRegistryService(get_job_registry(), lifecycle=lifecycle)
        return job_service

    def get_workflow_engine() -> ResumableWorkflowEngine:
        nonlocal workflow_engine
        if workflow_engine is None:
            from ..platform.paths import state_path

            workflow_engine = ResumableWorkflowEngine(
                get_job_service(),
                SQLiteWorkflowCheckpointRepository(
                    state_path("jobs.db", "SONDER_JOBS_DB")
                ),
            )
        return workflow_engine

    def get_agent_registry() -> UnifiedAgentRegistryService:
        nonlocal agent_registry
        if agent_registry is None:
            agent_registry = UnifiedAgentRegistryService(FleetStoreRegistryAdapter())
            agent_registry.register_workbench_modes()
        return agent_registry

    def get_extension_registry() -> ExtensionRegistry:
        nonlocal extension_registry
        if extension_registry is None:
            extension_registry = ExtensionRegistry()
        return extension_registry

    def get_experiment_manager() -> EphemeralExperimentManager:
        nonlocal experiment_manager
        if experiment_manager is None:
            # The application service owns lifecycle state and receives only
            # the typed child-host boundary from this composition root.  No
            # experiment may start unless an entrypoint explicitly supplies
            # an authority; the default is deliberately fail-closed.
            authorize = (
                extension_startup_authority
                if extension_startup_authority is not None
                else (lambda _definition: False)
            )

            def host_factory(definition, directory):
                return ExtensionHost(
                    definition.argv,
                    limits=definition.limits,
                    cwd=directory,
                    env=dict(definition.environment),
                )

            experiment_manager = EphemeralExperimentManager(
                authorize,
                host_factory=host_factory,
            )
        return experiment_manager

    return Application(
        profile=profile,
        runtime_policy=RuntimePolicyService(RuntimePolicyRepository()),
        model_gateway=gateway,
        chat=ChatService(
            gateway,
            session_capture_service,
            session_capture_factory=(
                None
                if session_capture_service is not None
                else get_session_capture_service
            ),
        ),
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
        session_capture_service=get_session_capture_service,
        compaction_service=get_compaction_service,
        job_registry=get_job_registry,
        job_service=get_job_service,
        workflow_engine=get_workflow_engine,
        agent_registry=get_agent_registry,
        config=config,
        vision=vision,
        web_provider=LegacyWebProvider(),
        extension_registry=get_extension_registry,
        experiment_manager=get_experiment_manager,
    )


_default_config: SonderConfig | None = None


def _build_default_application() -> Application:
    if _default_config is None:
        return build_application()
    return build_application(config=_default_config)


_application_lifecycle = ApplicationLifecycle(_build_default_application)


def default_app(*, config: SonderConfig | None = None) -> Application:
    """Process-wide default graph for compatibility shims."""
    global _default_config
    if config is not None:
        if not isinstance(config, SonderConfig):
            raise TypeError("config must be a SonderConfig when provided")
        if _default_config is not config:
            # Explicit entrypoint configuration is startup authority.  Reset
            # the lazy compatibility cache so repeated in-process CLI calls
            # cannot retain a prior command's config object.
            _default_config = config
            _application_lifecycle.reset()
    application = _application_lifecycle.get()
    if config is not None and application.config is not config:
        raise RuntimeError("default application was already built without this config")
    return application


def reset_for_tests() -> None:
    global _default_config
    _default_config = None
    _application_lifecycle.reset()
    runtime_paths.reset_home()
    ollama_endpoint.reset_typed_endpoint()
