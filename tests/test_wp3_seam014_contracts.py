"""Contract coverage for WP3 SEAM-014 specialized lifecycle ports."""
from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.specialized_lifecycle import (
    ActivationRequest,
    ActivationResult,
    CleanupResult,
    DeploymentResult,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResult,
    HealthReport,
    HealthStatus,
    TrainingBackend,
    TrainingRequest,
    UpdateActivator,
)


class FakeEmbedding:
    provider_id = "fake-embedding"

    def health(self):
        return HealthReport(self.provider_id, HealthStatus.HEALTHY)

    def cancel(self, *, reason="cancellation requested"):
        return True

    def cleanup(self, timeout=None):
        return CleanupResult(self.provider_id, True, True)

    def embed(self, request, context):
        return EmbeddingResult(self.provider_id, request.model, ())


class FakeTraining(FakeEmbedding):
    provider_id = "fake-training"

    def train(self, request, context):
        return DeploymentResult(self.provider_id, request.run_id, "d1", "m1", "a" * 64)


class FakeActivator(FakeEmbedding):
    provider_id = "fake-updates"

    def activate(self, request, context):
        return ActivationResult(
            self.provider_id, request.activation_id, request.release_id,
            request.version, request.artifact_digest,
        )


def test_contracts_are_runtime_checkable_by_structural_shape():
    assert isinstance(FakeEmbedding(), EmbeddingProvider)
    assert isinstance(FakeTraining(), TrainingBackend)
    assert isinstance(FakeActivator(), UpdateActivator)


def test_operations_receive_context_and_return_immutable_results():
    context = local_owner_context(correlation_id="seam-014")
    assert FakeTraining().train(
        TrainingRequest("run-1", "base", "rev", "d"), context
    ).run_id == "run-1"
    activation = FakeActivator().activate(
        ActivationRequest("act-1", "rel-1", "1.0.0", "a" * 64), context
    )
    assert activation.version == "1.0.0"
    with pytest.raises(FrozenInstanceError):
        activation.version = "2.0.0"


def test_all_boundary_dtos_are_frozen_dataclasses():
    dtos = (
        ActivationRequest, ActivationResult, CleanupResult, DeploymentResult,
        EmbeddingRequest, EmbeddingResult, HealthReport, TrainingRequest,
    )
    assert all(is_dataclass(dto) and dto.__dataclass_params__.frozen for dto in dtos)


def test_cleanup_reports_quiescence_and_cancellation_is_cooperative():
    provider = FakeEmbedding()
    assert provider.cancel(reason="shutdown") is True
    cleanup = provider.cleanup(timeout=0.1)
    assert cleanup.quiescent and cleanup.resources_released
