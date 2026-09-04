"""Deterministic composition root (SPEC-3 R-M8).

Runtime variants are assembled here — never through import-time global
initialization. Importing this module creates no directories, opens no
databases, reads no mutable environment state, starts no threads, probes
no hardware, and contacts no services; construction happens inside
``build_application`` and services stay lazy until first use.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

from ..adapters.persistence.autopilot_repository import AutopilotRepository
from ..adapters.persistence.fleet_registry import FleetStoreRegistryAdapter
from ..adapters.persistence.session_repository import SQLiteSessionRepository
from ..adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from ..adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from ..adapters.subagents import LocalSubagentProvider
from ..adapters.persistence.sqlite.workflow_checkpoints import SQLiteWorkflowCheckpointRepository
from ..adapters.process_probe import ProcessProbeAdapter
from ..adapters.process_termination import ProcessTreeSupervisor
from ..adapters.execution.process_jobs import SubprocessJobProvider
from ..application.execution.world_providers import (
    ContainerWorldConfig,
    GuardedContainerWorld,
    ConfiguredRemoteWorld,
    RemoteWorldConfig,
)
from ..adapters.execution.durable_output import DurableExecutionOutput, SQLiteSpillStore
from ..adapters.runtime_policy_repository import RuntimePolicyRepository
from ..adapters.tool_executor import ToolExecutorAdapter
from ..adapters.typed_tool_executor import PackagedToolExecutor
from ..adapters.persistence.tool_audit import DurableToolAuditRepository, ToolAuditLimits
from ..adapters.security.permission_evaluator import PermissionModesEvaluator
from ..application.tools.facade import PatternOutputRedactor, ReceiptStore, ToolApplicationFacade
from .typed_tools import POLICY_NAMES, typed_tool_policy, typed_tool_registry
from ..adapters.unit_of_work import UnitOfWorkAdapter
from ..adapters.operations_event_sink import OperationsEventSink
from ..adapters.security import permission_receipts
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
from ..adapters.provider_bindings import ProviderBindings, provider_bindings_from_env
from ..adapters.web_provider import LegacyWebProvider
from ..adapters.inference.ollama_vision import OllamaVisionGateway
from ..adapters.vision_input import FileVisionInputProvider
from ..adapters.application_lifecycle import ApplicationLifecycle
from ..adapters.extensions.host import ExtensionHost
from ..adapters.persistence.sqlite.extensions import SQLiteExtensionStateRepository
from ..adapters.system_clock import SystemClock
from ..application.chat.handle_chat import ChatService
from ..application.vision import VisionService
from ..application.session import (
    SessionCaptureService, SessionCheckpointPrivacyService, SessionContinuityService,
)
from ..application.session.http_facade import HttpSessionFacade
from ..application.compaction import SessionCompactionService
from ..application.extensions.experiments import (
    EphemeralExperimentManager,
    StartupAuthority,
)
from ..application.extensions.registry import ExtensionRegistry
from ..application.extensions.provenance_inventory import ProvenanceInventory
from ..application.selfmod.selfmod_service import GuardedLegacySelfmodService
from ..application.extensions.facade import ExtensionApplicationFacade
from ..application.backup import BackupService
from ..application.capabilities.jobs import JobRegistryService, ResumableWorkflowEngine
from ..application.jobs.durable_registry import JobRecoveryReport
from ..application.jobs.session_lifecycle import JobRegistryLifecycleAdapter, JobSessionLifecycleRecorder
from ..application.agent_registry.unified import UnifiedAgentRegistryService
from ..application.agents.delegation_service import DelegationService
from ..application.agents.durable_lineage import DurableLineageQuery
from ..application.agents.workflow_integration import AgentWorkflowService
from ..application.subagents.durable_continuation import DurableContinuationService
from ..application.evaluation_history import EvaluationHistoryService
from ..application.inspection import InspectionService
from ..application.recall import RecallService
from ..application.memory import MemoryLearningFacade
from ..application.preferences import PreferenceService
from ..application.ports.preferences import (
    ConnectionFactory,
    PreferenceModuleProvider,
)
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.ports.specialized_lifecycle import (
    ActivationRequest,
    ActivationResult,
    DeploymentResult,
    TrainingRequest,
)
from ..application.provider_overrides import ProviderOverrideService
from ..application.providers import (
    EmbeddingLifecycleAdapter,
    ProviderLifecycleError,
    ScopedProviderRegistry,
    SpecializedProviderBundle,
    TrainingLifecycleAdapter,
    UpdateLifecycleAdapter,
    wire_specialized_providers,
)
from ..domain.provider_override_policy import ProviderOverridePolicy
from ..domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    ComputePlacementScheduler,
    NodeSnapshot,
    NodeHealth,
    WorkloadKind,
)
from ..application.compute_fabric.jobs import (
    ArgumentPolicy,
    ComputeJobWorker,
    JobCatalogEntry,
)
from ..application.compute_fabric.registry import ComputeNodeRegistry
from ..application.compute_fabric.service import ComputeFabricService
from ..application.ports.web import WebProvider
from ..application.ports.session_repository import SessionRepository
from ..application.ports.jobs import JobRegistry
from ..application.execution.process_jobs import ProcessJobProvider
from ..application.ports.process_probe import ProcessProbe
from ..application.ports.repositories import AutomationRepository, UnitOfWork
from ..application.ports.tool_executor import ToolExecutor
from ..application.runtime_policy.use_cases import RuntimePolicyService
from ..application.workflows.use_cases import WorkflowService
from ..application.context_integration import ContextPlanningFacade
from ..application.control_plane import ControlPlaneSnapshotService
from ..application.context import OperationContext
from ..platform.config import SonderConfig
from ..platform import paths as runtime_paths
from ..adapters.inference import ollama_endpoint

PROFILES = ("workstation-local", "server-private")


@dataclass(frozen=True)
class Application:
    profile: str
    runtime_policy: RuntimePolicyService
    provider_bindings: ProviderBindings
    model_gateway: ModelGateway
    provider_registry: ScopedProviderRegistry
    provider_overrides: ProviderOverrideService
    specialized_providers: SpecializedProviderBundle
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
    memory: MemoryLearningFacade
    evaluation_history: EvaluationHistoryService
    preferences: PreferenceService
    workflows: WorkflowService
    session_repository: Callable[[], SessionRepository]
    session_capture_service: Callable[[], SessionCaptureService]
    session_checkpoint_privacy_service: Callable[[], SessionCheckpointPrivacyService]
    session_continuity_service: Callable[[], SessionContinuityService]
    session_http_facade: Callable[[], HttpSessionFacade]
    job_registry: Callable[[], JobRegistry]
    job_service: Callable[[], JobRegistryService]
    process_job_provider: Callable[[], ProcessJobProvider] | None = None
    job_recovery: Callable[..., JobRecoveryReport] | None = None
    config: SonderConfig | None = None
    vision: VisionService | None = None
    web_provider: WebProvider | None = None
    workflow_engine: Callable[[], ResumableWorkflowEngine] | None = None
    agent_registry: Callable[[], UnifiedAgentRegistryService] | None = None
    compaction_service: Callable[[], SessionCompactionService] | None = None
    extension_registry: Callable[[], ExtensionRegistry] | None = None
    experiment_manager: Callable[[], EphemeralExperimentManager] | None = None
    extension_facade: Callable[[], ExtensionApplicationFacade] | None = None
    selfmod_service: Callable[[], GuardedLegacySelfmodService] | None = None
    context_planning: ContextPlanningFacade | None = None
    control_plane_snapshot_service: ControlPlaneSnapshotService | None = None
    compute_registry: Callable[[], ComputeNodeRegistry] | None = None
    compute_snapshot: Callable[[], NodeSnapshot] | None = None
    compute_scheduler: ComputePlacementScheduler | None = None
    compute_job_worker: Callable[[], ComputeJobWorker] | None = None
    compute_service: Callable[[], ComputeFabricService] | None = None
    delegation_service: Callable[[], DelegationService] | None = None
    agent_workflow_service: Callable[[], AgentWorkflowService] | None = None
    lineage_query: Callable[[], DurableLineageQuery] | None = None
    # The typed tool boundary: the read-only workbench family runs through it
    # on every surface, with the runtime's permission modes as its evaluator
    # and operations-grade durable receipts (see bootstrap/typed_tools.py).
    tools: ToolApplicationFacade | None = None
    container_world_provider: Any | None = None
    remote_world_provider: Any | None = None

    def provider_health(self):
        """Return a typed, fail-closed snapshot of published provider health."""
        return tuple(
            self.provider_registry.health(item.provider_id)
            for item in self.provider_registry.providers()
        )

    def provider_health_data(self):
        """Return a redacted operator projection of published provider health."""
        rows = []
        for item in self.provider_registry.providers():
            try:
                report = self.provider_registry.health(item.provider_id)
                rows.append({
                    "provider_id": report.provider_id,
                    "status": report.status.value,
                    "detail": report.detail,
                    "checked_at": report.checked_at,
                })
            except Exception as exc:
                # A health probe must never make the control-plane status
                # endpoint disappear or imply readiness from an exception.
                logger.error(f"provider health probe failed for provider_id={item.provider_id!r}, reporting as unhealthy", exc_info=True)
                logger.warning(f"provider health probe failed for provider_id={item.provider_id!r}, reporting as unhealthy: {type(exc).__name__}")
                rows.append({
                    "provider_id": item.provider_id,
                    "status": "unhealthy",
                    "detail": f"health probe failed: {type(exc).__name__}",
                    "checked_at": "",
                })
        return tuple(rows)

    def cancel_provider(
        self, provider_id: str, *, reason: str = "cancellation requested",
    ) -> bool:
        """Request cooperative cancellation through the composed provider port."""
        return self.provider_registry.cancel(provider_id, reason=reason)

    def train_provider(
        self,
        request: TrainingRequest,
        context: OperationContext,
        *,
        provider_id: str = "training",
        scopes: Sequence[str] | None = None,
    ) -> DeploymentResult:
        """Run an attended training operation through the provider boundary."""
        provider = self.provider_registry.resolve(provider_id, scopes).provider
        operation = getattr(provider, "train", None)
        if not callable(operation):
            raise ProviderLifecycleError(
                f"provider {provider_id!r} does not support training"
            )
        result = operation(request, context)
        if not isinstance(result, DeploymentResult):
            raise ProviderLifecycleError("training provider returned an invalid result")
        return result

    def activate_provider(
        self,
        request: ActivationRequest,
        context: OperationContext,
        *,
        provider_id: str = "update",
        scopes: Sequence[str] | None = None,
    ) -> ActivationResult:
        """Activate a verified release through the provider boundary."""
        provider = self.provider_registry.resolve(provider_id, scopes).provider
        operation = getattr(provider, "activate", None)
        if not callable(operation):
            raise ProviderLifecycleError(
                f"provider {provider_id!r} does not support activation"
            )
        result = operation(request, context)
        if not isinstance(result, ActivationResult):
            raise ProviderLifecycleError("update provider returned an invalid result")
        return result

    def close_providers(self, timeout: float | None = None) -> None:
        """Quiesce and unpublish composed providers before process shutdown."""
        self.specialized_providers.close(timeout=timeout)


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
    unrestricted_selfmod: bool = False,
    embedding_provider=None,
    training_backend=None,
    update_activator=None,
    extension_provenance: ProvenanceInventory | None = None,
    control_plane_snapshot_service: ControlPlaneSnapshotService | None = None,
) -> Application:
    """Assemble one application graph for the selected profile.

    Entry points call this exactly once. As SPEC-3 phases extract more
    bounded contexts, their services join this graph; until then the
    legacy adapters wrap the root modules.
    """
    logger.info(f"build_application starting, profile={profile!r}")
    logger.debug(f"build_application starting, profile={profile!r}, config_provided={config is not None}")
    if config is not None:
        if not isinstance(config, SonderConfig):
            raise TypeError("config must be a SonderConfig when provided")
        profile = config.profile
        logger.info(f"config applied, effective profile={profile!r}")
        logger.debug(f"config supplied, effective profile={profile!r}, home={config.state.home!r}")
        if config.state.home:
            # Keep typed startup state process-local.  This must happen before
            # any lazy persistence factory can resolve a database path, and
            # must not be translated through the mutable process environment.
            runtime_paths.configure_home(config.state.home)
        ollama_endpoint.configure_typed_endpoint(config.ollama.url)
        from ..adapters.inference import ollama_pool
        logger.info(f"ollama pool configured, workers={len(config.ollama.workers)}, allow_remote={config.ollama.allow_remote}")
        logger.debug(f"configuring ollama pool: workers={len(config.ollama.workers)}, allow_remote={config.ollama.allow_remote}")
        ollama_pool.configure_typed_workers(
            config.ollama.workers,
            allow_remote=config.ollama.allow_remote,
            trusted_origins=config.ollama.trusted_origins,
            failure_threshold=config.ollama.worker_failure_threshold,
            cooldown_seconds=config.ollama.worker_cooldown_seconds,
            admission_timeout_ms=config.ollama.worker_admission_timeout_ms,
            capability_ttl_seconds=config.ollama.worker_capability_ttl_seconds,
            probe_timeout_ms=config.ollama.worker_probe_timeout_ms,
        )
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected {PROFILES}")
    logger.debug("configuring runtime lifecycle")
    from ..adapters.web import lifecycle as runtime_lifecycle
    runtime_lifecycle.configure(config)
    # Keep the transitional provider behind lazy closures: composing the
    # application must not import the historical root module.
    logger.debug("resolving legacy model provider factories")
    from .legacy_model import lazy_legacy_model_provider_factories
    target_resolver, generate_factory = lazy_legacy_model_provider_factories()
    # SPEC-3 Phase 3: the real transport adapter behind the port — consent
    # enforced against the OperationContext, driver errors mapped into the
    # domain taxonomy. Backend is Ollama by default, selectable via env.
    provider_registry = ScopedProviderRegistry()

    # The default embedding delegate is the existing local adapter.  It is
    # wrapped by the typed lifecycle shell so cancellation/deadline and
    # publication health are visible before it reaches the model gateway.
    if embedding_provider is None:
        logger.warning("no embedding provider supplied, falling back to local legacy adapter")
        import sonder_runtime.adapters.embeddings as legacy_embeddings

        def embedding_provider(request, context):
            timeout = context.remaining_seconds
            timeout = 30.0 if timeout is None else max(0.001, timeout)
            vectors = []
            for text in request.texts:
                if context.cancellation.cancelled or context.expired:
                    break
                vector = legacy_embeddings.embed(text, timeout=timeout, model=request.model or None)
                if vector is None:
                    raise RuntimeError("embedding dependency unavailable")
                vectors.append(vector)
            return vectors

    logger.debug(f"wiring provider adapters: embedding=custom({embedding_provider is not None}), training={training_backend is not None}, update={update_activator is not None}")
    embedding_adapter = EmbeddingLifecycleAdapter(embedding_provider)
    training_adapter = TrainingLifecycleAdapter(training_backend) if training_backend is not None else None
    update_adapter = UpdateLifecycleAdapter(update_activator) if update_activator is not None else None
    # Absent attended backends remain unpublished rather than represented by a
    # fake provider.  This preserves the existing fail-closed policy.
    # Partial publication is intentional: absent attended training/update
    # backends remain absent and therefore fail closed.  If one is supplied,
    # require the other so the production path cannot be half-composed.
    if (training_adapter is None) != (update_adapter is None):
        logger.critical(f"provider composition is internally contradictory: training_adapter={training_adapter is not None}, update_adapter={update_adapter is not None} -- they must be supplied together")
        raise ValueError("training_backend and update_activator must be supplied together")
    logger.debug("wiring specialized providers into registry")
    specialized_bundle = wire_specialized_providers(
        provider_registry,
        embedding=embedding_adapter,
        training=training_adapter,
        update=update_adapter,
    )

    logger.debug("resolving provider bindings from environment")
    try:
        provider_bindings = provider_bindings_from_env()
    except ValueError as exc:
        from ..domain.common.errors import InvalidInput

        raise InvalidInput(str(exc)) from exc
    logger.info(f"provider bindings resolved, provider={provider_bindings.default_generation_provider!r}")
    logger.debug("building model gateway")
    gateway = build_model_gateway(
        provider_bindings,
        target_resolver=target_resolver,
        generate_factory=generate_factory,
        embedding_provider=embedding_adapter,
    )
    logger.info("model gateway built")
    logger.debug("composing vision service and context planning facade")
    vision = VisionService(
        FileVisionInputProvider(),
        OllamaVisionGateway(target_resolver=target_resolver),
    )
    # Provider-neutral context planning is part of the live application graph;
    # hardware measurements remain an explicit adapter input at call time.
    context_planning = ContextPlanningFacade()
    session_repository: SQLiteSessionRepository | None = None
    canonical_session_capture: SessionCaptureService | None = session_capture_service
    compaction_service: SessionCompactionService | None = None
    session_checkpoint_privacy: SessionCheckpointPrivacyService | None = None
    session_continuity: SessionContinuityService | None = None
    session_http: HttpSessionFacade | None = None
    job_registry: SQLiteDurableJobRegistry | None = None
    job_service: JobRegistryService | None = None
    process_job_provider: ProcessJobProvider | None = None
    process_cleanup: ProcessTreeSupervisor | None = None
    spill_output: DurableExecutionOutput | None = None
    workflow_engine: ResumableWorkflowEngine | None = None
    agent_registry: UnifiedAgentRegistryService | None = None
    extension_registry: ExtensionRegistry | None = None
    experiment_manager: EphemeralExperimentManager | None = None
    extension_facade: ExtensionApplicationFacade | None = None
    selfmod_service: GuardedLegacySelfmodService | None = None
    compute_registry: ComputeNodeRegistry | None = None
    compute_snapshot_source = None
    compute_job_worker: ComputeJobWorker | None = None
    compute_service: ComputeFabricService | None = None
    compute_remote_snapshot_source = None
    compute_remote_transport = None
    effective_config = config or SonderConfig()
    compute_scheduler = ComputePlacementScheduler(
        snapshot_ttl=timedelta(seconds=effective_config.compute.snapshot_ttl_seconds)
    )
    continuation_repository: SQLiteDurableContinuationRepository | None = None
    continuation_service: DurableContinuationService | None = None
    subagent_provider: LocalSubagentProvider | None = None
    delegation: DelegationService | None = None
    agent_workflow: AgentWorkflowService | None = None
    lineage: DurableLineageQuery | None = None

    def get_session_repository() -> SessionRepository:
        nonlocal session_repository
        if session_repository is None:
            from ..platform.paths import state_path

            database = os.environ.get("SONDER_SESSIONS_DB", "").strip()
            if not database:
                database = state_path("sessions.db", "SONDER_SESSIONS_DB")
            logger.debug(f"lazy-init session repository at {database!r}")
            logger.info("session repository initialized")
            session_repository = SQLiteSessionRepository(
                database
            )
        return session_repository

    def get_job_registry() -> JobRegistry:
        nonlocal job_registry
        if job_registry is None:
            from ..platform.paths import state_path
            db_path = state_path("jobs.db", "SONDER_JOBS_DB")
            logger.debug(f"lazy-init job registry at {db_path!r}")
            logger.info("job registry initialized")
            job_registry = SQLiteDurableJobRegistry(db_path)
        return job_registry

    def get_session_capture_service() -> SessionCaptureService:
        nonlocal canonical_session_capture
        if canonical_session_capture is None:
            logger.debug("lazy-init session capture service")
            canonical_session_capture = SessionCaptureService(get_session_repository())
        return canonical_session_capture

    def get_compaction_service() -> SessionCompactionService:
        nonlocal compaction_service
        if compaction_service is None:
            compaction_service = SessionCompactionService(get_session_repository())
        return compaction_service

    def get_session_checkpoint_privacy_service() -> SessionCheckpointPrivacyService:
        nonlocal session_checkpoint_privacy
        if session_checkpoint_privacy is None:
            from ..adapters.persistence.session_checkpoint_privacy import (
                build_session_checkpoint_privacy_adapter,
            )

            session_checkpoint_privacy = build_session_checkpoint_privacy_adapter(
                get_session_repository()
            )
        return session_checkpoint_privacy

    def get_session_http_facade() -> HttpSessionFacade:
        nonlocal session_http
        if session_http is None:
            session_http = HttpSessionFacade(
                get_session_repository(), max_replay_events=1_000,
                continuity=get_session_continuity_service(),
            )
        return session_http

    def get_session_continuity_service() -> SessionContinuityService:
        nonlocal session_continuity
        if session_continuity is None:
            session_continuity = SessionContinuityService(
                get_session_repository(), get_session_checkpoint_privacy_service(),
            )
        return session_continuity

    def get_job_service() -> JobRegistryService:
        nonlocal job_service, process_cleanup
        if job_service is None:
            logger.debug("lazy-init job registry service")
            logger.info("job registry service initialized")
            if process_cleanup is None:
                process_cleanup = ProcessTreeSupervisor()
            lifecycle = JobRegistryLifecycleAdapter(
                JobSessionLifecycleRecorder(get_session_repository())
            )
            job_service = JobRegistryService(
                get_job_registry(),
                process_cleanup=process_cleanup,
                lifecycle=lifecycle,
            )
        return job_service

    def get_process_job_provider() -> ProcessJobProvider:
        nonlocal process_job_provider, process_cleanup, spill_output
        if process_job_provider is None:
            logger.debug("lazy-init subprocess job provider")
            logger.info("subprocess job provider initialized")
            from ..platform.paths import state_path
            if process_cleanup is None:
                process_cleanup = ProcessTreeSupervisor()
            spill_output = DurableExecutionOutput(
                SQLiteSpillStore(state_path("execution-spill.db", "SONDER_JOBS_DB"))
            )
            process_job_provider = SubprocessJobProvider(
                get_job_registry(),
                process_cleanup=process_cleanup,
                lifecycle=get_job_service()._lifecycle,
                output=spill_output,
                max_concurrent_processes=effective_config.capacity.tool_processes,
            )
        return process_job_provider

    def get_compute_registry() -> ComputeNodeRegistry:
        nonlocal compute_registry
        if compute_registry is None:
            logger.debug(f"lazy-init compute registry, node_id={effective_config.compute.node_id!r}, remote_nodes={len(effective_config.compute.nodes)}")
            logger.info(f"compute registry initialized, node_id={effective_config.compute.node_id!r}, remote_nodes={len(effective_config.compute.nodes)}")
            local_workloads = frozenset(
                item for item in WorkloadKind if item is not WorkloadKind.INFERENCE
            )
            mapping_names = {"default"}
            mapping_names.update(
                Path(root).name
                for root in effective_config.state.workspace_roots
                if Path(root).name
            )
            local = ComputeNode(
                node_id=effective_config.compute.node_id,
                origin=None,
                local=True,
                allowed_workloads=local_workloads,
                configured_capabilities=frozenset(ComputeCapability),
                workspace_mappings=frozenset(mapping_names),
            )
            remote = tuple(
                ComputeNode(
                    node_id=node.node_id,
                    origin=node.origin,
                    local=False,
                    allowed_workloads=frozenset(
                        WorkloadKind(item) for item in node.workloads
                    ),
                    configured_capabilities=frozenset(
                        ComputeCapability(item) for item in node.capabilities
                    ),
                    workspace_mappings=frozenset(node.workspace_mappings),
                    preference_weight=node.preference_weight,
                )
                for node in effective_config.compute.nodes
            )
            compute_registry = ComputeNodeRegistry(
                (local, *remote),
                snapshot_ttl=timedelta(
                    seconds=effective_config.compute.snapshot_ttl_seconds
                ),
            )
        return compute_registry

    def get_compute_snapshot() -> NodeSnapshot:
        nonlocal compute_snapshot_source
        if compute_snapshot_source is None:
            from ..adapters.compute_fabric.local_snapshot import (
                LocalComputeSnapshotSource,
            )

            storage_path = Path(
                effective_config.state.home or runtime_paths.default_home()
            )
            compute_snapshot_source = LocalComputeSnapshotSource(
                storage_path=storage_path,
                active_jobs=lambda: sum(
                    1
                    for record in get_job_service().list(limit=1024)
                    if record.status.value in {"pending", "running", "cancelling"}
                ),
            )
        registry = get_compute_registry()
        snapshot = compute_snapshot_source.snapshot(
            registry.get_node(effective_config.compute.node_id),
            now=datetime.now(timezone.utc),
        )
        return registry.observe(snapshot)

    def get_compute_job_worker() -> ComputeJobWorker:
        nonlocal compute_job_worker
        if compute_job_worker is None:
            catalog = {
                job.job_id: JobCatalogEntry(
                    entry_id=job.job_id,
                    workload=WorkloadKind(job.workload),
                    program=job.program,
                    fixed_args=job.fixed_args,
                    argument_policy=ArgumentPolicy(job.argument_policy),
                    environment_allowlist=frozenset(job.environment_allowlist),
                    workspace_mappings=frozenset(job.workspace_mappings),
                    allowed_flags=frozenset(job.allowed_flags),
                    allowed_bounded_options=frozenset(job.allowed_bounded_options),
                    allowed_relative_path_options=frozenset(
                        job.allowed_relative_path_options
                    ),
                    memory_limit_bytes=job.memory_limit_bytes,
                    memory_reservation_bytes=job.memory_reservation_bytes,
                    artifact_paths=job.artifact_paths,
                )
                for job in effective_config.compute.jobs
            }
            roots: dict[str, Path] = {}
            for raw_root in effective_config.state.workspace_roots:
                root = Path(raw_root).resolve()
                if root.name in roots:
                    raise ValueError(
                        "compute workspace root basenames must be unique"
                    )
                roots[root.name] = root
            roots["default"] = (
                Path(effective_config.state.workspace_roots[0]).resolve()
                if effective_config.state.workspace_roots
                else Path.cwd().resolve()
            )
            from ..application.compute_fabric.capacity import WorkerBudget, measured_worker_budget

            def worker_budget() -> WorkerBudget:
                configured = effective_config.compute.worker_memory_budget_bytes
                if configured is None:
                    return measured_worker_budget(
                        get_compute_snapshot(), effective_config.compute.worker_host_id,
                    )
                return WorkerBudget(
                    effective_config.compute.worker_host_id, configured,
                    effective_config.compute.worker_max_jobs,
                )

            compute_job_worker = ComputeJobWorker(
                worker_id=effective_config.compute.node_id,
                catalog=catalog,
                workspace_mappings=roots,
                provider=get_process_job_provider(),
                capacity=get_job_registry(),
                budget=worker_budget,
                reservation_seconds=effective_config.compute.worker_reservation_seconds,
            )
        return compute_job_worker

    def refresh_compute_snapshots() -> None:
        nonlocal compute_remote_snapshot_source
        get_compute_snapshot()
        if not effective_config.compute.nodes:
            return
        if not effective_config.compute.allow_remote:
            return
        if not effective_config.secrets.api_key:
            raise ValueError("remote compute requires SONDER_API_KEY")
        if compute_remote_snapshot_source is None:
            from ..adapters.compute_fabric.http_client import (
                HttpsComputeSnapshotSource,
            )

            compute_remote_snapshot_source = HttpsComputeSnapshotSource(
                api_key=effective_config.secrets.api_key,
                timeout_seconds=effective_config.compute.probe_timeout_ms / 1000.0,
            )
        registry = get_compute_registry()
        from ..application.compute_fabric.refresh import refresh_remote_snapshots

        refresh_remote_snapshots(
            registry,
            compute_remote_snapshot_source,
            now=lambda: datetime.now(timezone.utc),
            max_workers=8,
        )

    def get_compute_service() -> ComputeFabricService:
        nonlocal compute_service, compute_remote_transport
        if compute_service is None:
            logger.debug(f"lazy-init compute fabric service, remote_nodes={len(effective_config.compute.nodes)}")
            logger.info(f"compute fabric service initialized, remote_nodes={len(effective_config.compute.nodes)}")
            if effective_config.compute.nodes and not effective_config.secrets.api_key:
                raise ValueError("remote compute requires SONDER_API_KEY")
            if compute_remote_transport is None:
                from ..adapters.compute_fabric.http_client import (
                    HttpsComputeJobTransport,
                )

                if not effective_config.secrets.api_key:
                    logger.warning("compute transport initialized without API key, remote dispatch will fail")
                compute_remote_transport = HttpsComputeJobTransport(
                    api_key=(
                        effective_config.secrets.api_key
                        or "local-compute-only"
                    ),
                )
            compute_service = ComputeFabricService(
                registry=get_compute_registry(),
                scheduler=compute_scheduler,
                transport=compute_remote_transport,
                local_worker=get_compute_job_worker(),
                now=lambda: datetime.now(timezone.utc),
                refresh=refresh_compute_snapshots,
                placement_registry=get_job_registry(),
                metrics=runtime_lifecycle.get().metrics,
            )
        return compute_service

    def recover_jobs(**kwargs: object) -> JobRecoveryReport:
        """Reconcile durable process jobs through the typed tree supervisor."""
        nonlocal process_cleanup
        if process_cleanup is None:
            process_cleanup = ProcessTreeSupervisor()
        return get_job_registry().reconcile_with_cleanup(process_cleanup, **kwargs)

    def get_workflow_engine() -> ResumableWorkflowEngine:
        nonlocal workflow_engine
        if workflow_engine is None:
            logger.debug("lazy-init resumable workflow engine")
            logger.info("resumable workflow engine initialized")
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
            logger.debug("lazy-init agent registry and registering workbench modes")
            logger.info("agent registry initialized")
            agent_registry = UnifiedAgentRegistryService(FleetStoreRegistryAdapter())
            agent_registry.register_workbench_modes()
        return agent_registry

    def _noop_runner(state, save, control):
        return ""

    def get_delegation_service() -> DelegationService:
        nonlocal continuation_repository, continuation_service, subagent_provider, delegation
        if delegation is None:
            from ..platform.paths import state_path
            db_path = state_path("child-sessions.db", "SONDER_CHILD_SESSIONS_DB")
            logger.debug(f"lazy-init delegation subsystem at {db_path!r}")
            continuation_repository = SQLiteDurableContinuationRepository(db_path)
            continuation_service = DurableContinuationService(continuation_repository)
            subagent_provider = LocalSubagentProvider(
                continuation_service, _noop_runner,
            )
            delegation = DelegationService(subagent_provider, events)
            logger.info("delegation service initialized")
        return delegation

    def get_agent_workflow_service() -> AgentWorkflowService:
        nonlocal agent_workflow
        if agent_workflow is None:
            agent_workflow = AgentWorkflowService(get_delegation_service())
            logger.info("agent workflow service initialized")
        return agent_workflow

    def get_lineage_query() -> DurableLineageQuery:
        nonlocal lineage
        if lineage is None:
            get_delegation_service()
            lineage = DurableLineageQuery(
                jobs=get_job_registry(),
                children=continuation_repository,
            )
            logger.info("lineage query service initialized")
        return lineage

    def get_extension_registry() -> ExtensionRegistry:
        nonlocal extension_registry
        if extension_registry is None:
            logger.debug("lazy-init extension registry")
            from ..platform.paths import state_path
            extension_registry = ExtensionRegistry(
                provenance=extension_provenance or ProvenanceInventory.build([]),
                repository=SQLiteExtensionStateRepository(state_path("extensions.db", "SONDER_EXTENSIONS_DB")),
            )
        return extension_registry

    def get_experiment_manager() -> EphemeralExperimentManager:
        nonlocal experiment_manager
        if experiment_manager is None:
            logger.debug(f"lazy-init experiment manager, startup_authority_provided={extension_startup_authority is not None}")
            # The application service owns lifecycle state and receives only
            # the typed child-host boundary from this composition root.  No
            # experiment may start unless an entrypoint explicitly supplies
            # an authority; the default is deliberately fail-closed.
            authorize = (
                extension_startup_authority
                if extension_startup_authority is not None
                else (lambda _definition: False)
            )
            if extension_startup_authority is None:
                logger.warning("no extension startup authority supplied, all experiment starts will be denied (fail-closed)")

            def host_factory(definition, directory):
                host_limits = definition.limits
                if host_limits is not None and hasattr(host_limits, "memory_limit_bytes"):
                    from ..adapters.extensions.host import ExtensionHostLimits
                    host_limits = ExtensionHostLimits(
                        memory_limit_bytes=host_limits.memory_limit_bytes
                    )
                return ExtensionHost(
                    definition.argv,
                    limits=host_limits,
                    cwd=directory,
                    env=dict(definition.environment),
                )

            experiment_manager = EphemeralExperimentManager(
                authorize,
                host_factory=host_factory,
            )
        return experiment_manager

    def get_extension_facade() -> ExtensionApplicationFacade:
        nonlocal extension_facade
        if extension_facade is None:
            extension_facade = ExtensionApplicationFacade(
                get_extension_registry(), get_experiment_manager()
            )
        return extension_facade

    def get_selfmod_service() -> GuardedLegacySelfmodService:
        nonlocal selfmod_service
        if selfmod_service is None:
            # The root module remains the mutation/recovery authority. Keep
            # its import lazy and behind this bootstrap-only port so the
            # application service remains free of legacy dependencies.
            class _LegacySelfmodModulePort:
                def __getattr__(self, name: str):
                    return getattr(importlib.import_module("selfmod"), name)

            if unrestricted_selfmod:
                logger.warning("self-modification service running in UNRESTRICTED mode, safety guards bypassed")
            selfmod_service = GuardedLegacySelfmodService(
                _LegacySelfmodModulePort(), unrestricted=unrestricted_selfmod,
            )
        return selfmod_service

    def unavailable_section(name: str):
        """Expose an honest section state when no owning port is composed."""
        return lambda: ({
            "available": False,
            "section": name,
            "reason": "owning application port is not composed",
        },)

    def session_section():
        events = get_session_repository().search(limit=1024)
        latest: dict[str, object] = {}
        for event in events:
            latest[event.session_id] = event
        return tuple({
            "session_id": event.session_id,
            "last_sequence": event.sequence,
            "last_event_type": event.event_type,
            "occurred_at_utc": event.occurred_at_utc,
        } for event in latest.values())

    def job_section():
        return tuple({
            "job_id": record.identity.job_id,
            "kind": record.identity.kind,
            "status": record.status.value,
            "revision": record.revision,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        } for record in get_job_service().list(limit=1024))

    def task_section():
        import sonder_runtime.adapters.memory_store as memory_store

        state_home = os.environ.get("SONDER_STATE_HOME", "").strip()
        database = (
            str(Path(state_home).expanduser() / "memory.db")
            if state_home else runtime_paths.memory_db_path()
        )
        connection = memory_store.connect(database)
        try:
            rows = memory_store.list_tasks(
                connection, limit=200, include_done=True,
            )
        finally:
            connection.close()
        return tuple({
            "id": row.get("id", ""),
            "title": row.get("title", ""),
            "status": row.get("status", ""),
            "priority": row.get("priority", 0),
            "project": row.get("project", ""),
            "owner": row.get("owner", ""),
            "parent_id": row.get("parent_id", ""),
        } for row in rows)

    def approval_section():
        from ..adapters.persistence import queued_actions

        connection = queued_actions.connect()
        try:
            records = queued_actions.list_actions(connection, limit=256)
        finally:
            connection.close()
        return tuple({
            "approval_id": record.id,
            "action_type": record.action_type,
            "proposed_by": record.proposed_by.value,
            "execution_scope": record.execution_scope,
            "state": record.state.value,
            "version": record.version,
            "created": record.created,
            "updated": record.updated,
        } for record in records)

    def context_section():
        assembly = context_planning.last_good()
        if assembly is None:
            return unavailable_section("context")()
        return ({
            "available": True,
            "model": assembly.plan.model,
            "input_budget_tokens": assembly.plan.input_budget_tokens,
            "selected_tokens": assembly.selected_tokens,
            "sections": {
                name: len(selection.selected)
                for name, selection in assembly.selections.items()
            },
            "has_prefix_manifest": assembly.prefix is not None,
            "has_replay_manifest": assembly.replay is not None,
        },)

    def memory_explanation_section():
        return tuple({
            "memory_class": memory_class.value,
            "write_min_confidence": policy.write_min_confidence,
            "write_requires_provenance": policy.write_requires_provenance,
            "write_requires_evidence_or_confirmation": (
                policy.write_requires_explicit_or_evidence
            ),
            "retrieval_enabled": policy.retrieval_enabled,
            "retrieval_requires_scope": policy.retrieval_requires_scope,
            "default_max_age_seconds": (
                policy.default_max_age.total_seconds()
                if policy.default_max_age is not None else None
            ),
            "promotion_enabled": policy.promotion_enabled,
            "export_allowed": policy.export_allowed,
            "deletion_allowed": policy.deletion_allowed,
            "private_by_default": policy.private_by_default,
        } for memory_class, policy in sorted(
            memory_facade.policy.policies.items(), key=lambda item: item[0].value
        ))

    def update_section():
        try:
            read_update_status = importlib.import_module(
                "sonder_runtime.adapters.updates.control_plane"
            ).read_update_status
            status = read_update_status()
        except Exception:
            logger.error("update status check failed, control plane module failed to load or execute", exc_info=True)
            logger.warning("update status unavailable, control plane module failed to load or execute")
            return unavailable_section("updates")()
        return ({
            "available": True,
            "running_version": status.get("running_version", ""),
            "running_commit": status.get("running_commit", ""),
            "platform": status.get("platform", ""),
            "plan_count": len(status.get("plans", ())),
            "has_active_release": status.get("active_release") is not None,
            "has_previous_release": status.get("previous_release") is not None,
        },)

    def selfmod_section():
        rows = get_selfmod_service().list_runs(limit=64)
        return tuple({
            "run_id": row.get("id", ""),
            "phase": row.get("phase", ""),
            "objective": str(row.get("objective", ""))[:256],
            "approved": bool(row.get("approval_given", False)),
            "tests_passed": bool(row.get("tests_passed", False)),
        } for row in rows)

    def startup_authority_section():
        experiments = get_experiment_manager().snapshot()
        return ({
            "authority": "extension_startup",
            "configured": True,
            "experiment_count": len(experiments),
            "running_count": sum(1 for item in experiments if item.state == "running"),
        },)

    memory_facade = MemoryLearningFacade(
        UnitOfWorkAdapter,
        recall_service=RecallService(LegacyRecallGateway()),
    )

    def agent_section():
        return tuple({
            "name": registration.name,
            "role": registration.role,
            "mutation_policy": registration.mutation_policy,
            "default_tier": registration.default_tier,
        } for registration in get_agent_registry().registrations)

    def provider_section():
        return tuple({
            "provider_id": report.provider_id,
            "status": report.status.value,
            "detail": report.detail,
            "checked_at": report.checked_at,
        } for report in (
            provider.provider.health()
            for provider in provider_registry.providers()
        ))

    def training_section():
        reports = tuple(
            provider.provider.health()
            for provider in provider_registry.providers()
            if "training" in provider.provider_id.casefold()
        )
        if not reports:
            return unavailable_section("training")()
        return tuple({
            "available": True,
            "provider_id": report.provider_id,
            "status": report.status.value,
            "detail": report.detail,
            "checked_at": report.checked_at,
        } for report in reports)

    def extension_section():
        return tuple({
            "extension_id": record.extension_id,
            "scope": record.scope.value,
            "project_id": record.project_id,
            "version": record.version,
            "enabled": record.enabled,
            "health_state": record.health_state.value,
            "crash_count": record.crash_count,
        } for record in get_extension_registry().snapshot().records)

    def compute_fabric_section():
        registry = get_compute_registry()
        try:
            get_compute_snapshot()
        except Exception as exc:
            logger.error(f"local compute snapshot probe failed, node health will report unknown", exc_info=True)
            logger.warning(f"local compute snapshot probe failed: {type(exc).__name__}, node health will report unknown")
            local_error = type(exc).__name__
        else:
            local_error = ""
        now = datetime.now(timezone.utc)
        rows = []
        for node in registry.configured_nodes():
            observed = registry.last_observation(node.node_id)
            rows.append({
                "node_id": node.node_id,
                "local": node.local,
                "configured": True,
                "observed": observed is not None,
                "stale": registry.is_stale(node.node_id, now=now),
                "health": observed.health.value if observed is not None else "unknown",
                "observed_at": (
                    observed.observed_at.isoformat() if observed is not None else None
                ),
                "received_at": (
                    observed.received_at.isoformat()
                    if observed is not None and observed.received_at is not None
                    else None
                ),
                "active_jobs": observed.active_jobs if observed is not None else None,
                "workloads": sorted(item.value for item in node.allowed_workloads),
                "capabilities": sorted(
                    item.value
                    for item in (
                        observed.effective_capabilities
                        if observed is not None else frozenset()
                    )
                ),
                "evidence_ref": observed.evidence_ref if observed is not None else None,
                "probe_error": (
                    local_error if node.local else registry.last_probe_error(node.node_id) or ""
                ),
            })
        return tuple(rows)

    def lineage_section():
        try:
            query = get_lineage_query()
            nodes = query.snapshot(limit=200)
            return tuple(
                {"node_id": n.node_id, "parent_id": n.parent_id, "root_id": n.root_id,
                 "kind": n.kind, "status": n.status, "depth": n.depth}
                for n in nodes
            )
        except Exception:
            logger.debug("lineage section snapshot failed", exc_info=True)
            return ()

    default_control_plane_service = control_plane_snapshot_service or ControlPlaneSnapshotService({
        "sessions": session_section,
        "plans": task_section,
        "approvals": approval_section,
        "jobs": job_section,
        "agents": agent_section,
        "model_hardware": provider_section,
        "context": context_section,
        "memory_explanations": memory_explanation_section,
        "extensions": extension_section,
        "training": training_section,
        "selfmod": selfmod_section,
        "updates": update_section,
        "health": provider_section,
        "startup_authorities": startup_authority_section,
        "compute_fabric": compute_fabric_section,
        "lineage": lineage_section,
    })

    # Bounded process-local counters/recent events decorate the durable
    # operations.db sink; they never replace its audit authority. Unattended
    # permission decisions leave their content-free receipts on this same
    # sink, replacing the default the legacy module installs when it loads
    # before a graph exists.
    events = LocalObservabilitySink(OperationsEventSink())
    permission_receipts.install(lambda: events)

    # The typed tool boundary for the workbench file families (the reads and
    # the mutations). The resource policy admits exactly those families; the
    # permission-modes evaluator makes the operator's standing policy the
    # second gate on every typed call that was not already decided by the
    # surface forwarding it; every receipt is durable before it is visible.
    logger.debug("composing typed tool application facade")
    from ..platform.logging import Redactor as _Redactor

    tools = ToolApplicationFacade.compose(
        typed_tool_registry(),
        PackagedToolExecutor(),
        policy=typed_tool_policy(),
        redactor=PatternOutputRedactor(_Redactor().redact),
        receipts=ReceiptStore(),
        audit=DurableToolAuditRepository(
            runtime_paths.state_path(
                os.path.join("audit", "tool-receipts.jsonl"), "SONDER_TOOL_AUDIT",
            ),
            limits=ToolAuditLimits(),
        ),
        permissions=(PermissionModesEvaluator(policy_names=POLICY_NAMES),),
    )

    logger.info(f"application graph assembled, profile={profile!r}")
    logger.debug(f"assembling Application graph for profile={profile!r}")
    return Application(
        profile=profile,
        runtime_policy=RuntimePolicyService(RuntimePolicyRepository()),
        provider_bindings=provider_bindings,
        model_gateway=gateway,
        provider_registry=provider_registry,
        provider_overrides=ProviderOverrideService(
            ProviderOverridePolicy({item.provider_id: item.provider_id for item in provider_registry.providers()})
        ),
        specialized_providers=specialized_bundle,
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
        tools=tools,
        process_probe=ProcessProbeAdapter(),
        events=events,
        clock=SystemClock(),
        backup=BackupService(LegacyBackupGateway()),
        inspections=InspectionService(InspectionExecutorAdapter()),
        recall=RecallService(LegacyRecallGateway()),
        memory=memory_facade,
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
        session_checkpoint_privacy_service=get_session_checkpoint_privacy_service,
        session_continuity_service=get_session_continuity_service,
        session_http_facade=get_session_http_facade,
        compaction_service=get_compaction_service,
        job_registry=get_job_registry,
        job_service=get_job_service,
        process_job_provider=get_process_job_provider,
        job_recovery=recover_jobs,
        workflow_engine=get_workflow_engine,
        agent_registry=get_agent_registry,
        config=config,
        vision=vision,
        web_provider=LegacyWebProvider(),
        extension_registry=get_extension_registry,
        experiment_manager=get_experiment_manager,
        extension_facade=get_extension_facade,
        selfmod_service=get_selfmod_service,
        context_planning=context_planning,
        control_plane_snapshot_service=default_control_plane_service,
        compute_registry=get_compute_registry,
        compute_snapshot=get_compute_snapshot,
        compute_scheduler=compute_scheduler,
        compute_job_worker=get_compute_job_worker,
        compute_service=get_compute_service,
        delegation_service=get_delegation_service,
        agent_workflow_service=get_agent_workflow_service,
        lineage_query=get_lineage_query,
        container_world_provider=GuardedContainerWorld(
            ContainerWorldConfig(world_id="default-container", image="sonder-sandbox:latest"),
        ),
        remote_world_provider=None,
    )


_default_config: SonderConfig | None = None


def _build_default_application() -> Application:
    if _default_config is None:
        return build_application()
    return build_application(config=_default_config)


_application_lifecycle = ApplicationLifecycle(_build_default_application)


def default_app(*, config: SonderConfig | None = None) -> Application:
    """Process-wide default graph for compatibility shims."""
    logger.debug(f"default_app called, config_provided={config is not None}")
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
        logger.critical("default application was already built with a different config object -- process-wide state is inconsistent and cannot be recovered")
        raise RuntimeError("default application was already built without this config")
    return application


def reset_for_tests() -> None:
    global _default_config
    _default_config = None
    _application_lifecycle.reset()
    runtime_paths.reset_home()
    ollama_endpoint.reset_typed_endpoint()
