"""Frozen application graph, independent of composition and legacy binding."""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic
from ..application.tools.facade import ToolApplicationFacade
from ..application.developer_tools import DeveloperToolServices
from ..application.debugging.service import DebugDigestService
from ..adapters.provider_bindings import ProviderBindings
from ..application.chat.handle_chat import ChatService
from ..application.vision import VisionService
from ..application.session import SessionCaptureService, SessionCheckpointPrivacyService, SessionContinuityService
from ..application.session.http_facade import HttpSessionFacade
from ..application.compaction import SessionCompactionService
from ..application.extensions.experiments import EphemeralExperimentManager
from ..application.extensions.registry import ExtensionRegistry
from ..application.selfmod.selfmod_service import GuardedLegacySelfmodService
from ..application.extensions.facade import ExtensionApplicationFacade
from ..application.backup import BackupService
from ..application.capabilities.jobs import JobRegistryService, ResumableWorkflowEngine
from ..application.jobs.durable_registry import JobRecoveryReport
from ..application.agent_registry.unified import UnifiedAgentRegistryService
from ..application.agents.delegation_service import DelegationService
from ..application.agents.durable_lineage import DurableLineageQuery
from ..application.agents.workflow_integration import AgentWorkflowService
from ..application.evaluation_history import EvaluationHistoryService
from ..application.evaluation.service import EvaluationApplicationService
from ..application.inspection import InspectionService
from ..application.recall import RecallService
from ..application.memory import MemoryLearningFacade
from ..application.preferences import PreferenceService
from ..application.ports.clock import Clock
from ..application.ports.event_sink import EventSink
from ..application.ports.model_gateway import ModelGateway
from ..application.ports.specialized_lifecycle import ActivationRequest, ActivationResult, DeploymentResult, TrainingRequest
from ..application.provider_overrides import ProviderOverrideService
from ..application.providers import ProviderLifecycleError, ScopedProviderRegistry, SpecializedProviderBundle
from ..domain.compute_fabric import ComputePlacementScheduler, NodeSnapshot
from ..application.compute_fabric.jobs import ComputeJobWorker
from ..application.compute_fabric.registry import ComputeNodeRegistry
from ..application.compute_fabric.service import ComputeFabricService
from ..application.ports.web import WebProvider
from ..application.ports.session_repository import SessionRepository
from ..application.ports.jobs import JobRegistry
from ..application.execution.process_jobs import ProcessJobProvider
from ..application.ports.process_probe import ProcessProbe
from ..application.ports.repositories import AutomationRepository, UnitOfWork
from ..application.ports.tool_executor import ToolExecutor
from ..application.tools.audit import ToolAuditRepository
from ..application.runtime_policy.use_cases import RuntimePolicyService
from ..application.workflows.use_cases import WorkflowService
from ..application.context_integration import ContextPlanningFacade
from ..application.control_plane import ControlPlaneSnapshotService
from ..application.context import OperationContext
from ..platform.config import SonderConfig

logger = logging.getLogger("sonder_runtime.bootstrap.app")


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
    evaluation_service: Callable[[], EvaluationApplicationService] | None = None
    # Fact-only trusted-peer replication is inert until explicitly invoked.
    memory_replication: Any | None = None
    process_job_provider: Callable[[], ProcessJobProvider] | None = None
    job_recovery: Callable[..., JobRecoveryReport] | None = None
    # Bounded verifier reconciliation of unresolved worker effects.  Runs once
    # during composition; operators may re-run it.  Never executes an effect.
    worker_effect_reconciliation: Callable[..., Any] | None = None
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
    agent_lanes: Callable[[], object] | None = None
    agent_workflow_service: Callable[[], AgentWorkflowService] | None = None
    lineage_query: Callable[[], DurableLineageQuery] | None = None
    # The typed tool boundary: the read-only workbench family runs through it
    # on every surface, with the runtime's permission modes as its evaluator
    # and operations-grade durable receipts (see bootstrap/typed_tools.py).
    tools: ToolApplicationFacade | None = None
    container_world_provider: Any | None = None
    remote_world_provider: Any | None = None
    compute_inventory_page: Callable[..., dict] | None = None
    compute_refresh_page: Callable[..., dict] | None = None
    close_compute: Callable[..., None] | None = None
    close_delegation: Callable[..., None] | None = None
    inference_pool: Any | None = None
    inference_membership: Any | None = None
    artifact_mobility_status: Callable[[str], dict] | None = None
    artifact_mobility_list: Callable[[], dict] | None = None
    close_artifact_mobility: Callable[[], None] | None = field(default=None, repr=False)
    _artifact_mobility_binding: Callable[[], object] | None = field(default=None, repr=False)
    _artifact_mobility_available: Callable[[], bool] | None = field(default=None, repr=False)
    # Shared with the typed gateway to record native MCP compatibility calls.
    tool_audit: ToolAuditRepository | None = field(default=None, repr=False)
    # Host tool inventory, structured test runs and the output digest
    # (bootstrap/developer_tools.py); None when this runtime did not compose
    # them, which every surface reports instead of failing.
    developer_tools: DeveloperToolServices | None = None
    # Crash and profile digests (bootstrap/debug_tools.py); None when this
    # runtime did not compose them, which every surface reports instead.
    debug_tools: "DebugDigestService | None" = None

    def operational_capabilities(self):
        from ..domain.operational_capabilities import build_operational_capabilities
        return build_operational_capabilities(
            config=self.config,
            fixed_peer_artifact_copy_configured=(
                self._artifact_mobility_available is not None
                and self._artifact_mobility_available()
            ),
        )

    @property
    def private_source_paths(self) -> tuple[str, ...]:
        """Exact host-loaded provenance, never inferred from diagnostic labels."""
        return self.config.private_source_paths if self.config is not None else ()

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
        """Quiesce every composed runtime resource before process shutdown."""
        started = monotonic()
        try:
            if self.close_artifact_mobility is not None:
                self.close_artifact_mobility()
        finally:
            try:
                try:
                    if self.close_delegation is not None:
                        self.close_delegation(timeout=timeout)
                finally:
                    if self.close_compute is not None:
                        remaining = None if timeout is None else max(
                            0, timeout - (monotonic() - started)
                        )
                        self.close_compute(timeout=remaining)
            finally:
                try:
                    if self.memory_replication is not None:
                        self.memory_replication.close()
                finally:
                    try:
                        # Fence inference admission before stopping membership
                        # refresh.  The controller's close only stops roster
                        # updates; without this drain a pool can still accept
                        # new requests while the surrounding graph is closed.
                        drain = getattr(self.inference_pool, "drain", None)
                        if callable(drain):
                            remaining = None if timeout is None else max(
                                0, timeout - (monotonic() - started)
                            )
                            if not drain(timeout_seconds=5 if remaining is None else min(30, remaining)):
                                raise TimeoutError("inference worker pool has not drained")
                    finally:
                        try:
                            remaining = None if timeout is None else max(
                                0, timeout - (monotonic() - started)
                            )
                            if self.inference_membership is not None and not self.inference_membership.close(
                                timeout=5 if remaining is None else min(30, remaining)
                            ):
                                raise TimeoutError("inference membership refresh has not stopped")
                        finally:
                            remaining = None if timeout is None else max(
                                0, timeout - (monotonic() - started)
                            )
                            self.specialized_providers.close(timeout=remaining)
