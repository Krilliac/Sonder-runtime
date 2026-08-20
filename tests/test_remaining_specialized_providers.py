"""SEAM-014 provider wiring and lifecycle integration tests."""
from __future__ import annotations

from threading import Event, Thread
import time

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import Embedding
from sonder_runtime.application.ports.specialized_lifecycle import (
    ActivationRequest,
    ActivationResult,
    CleanupResult,
    DeploymentResult,
    EmbeddingRequest,
    TrainingRequest,
)
from sonder_runtime.application.providers.lifecycle_registry import ScopedProviderRegistry
from sonder_runtime.application.providers.specialized_lifecycle import (
    EmbeddingLifecycleAdapter,
    SpecializedLifecycleError,
    TrainingLifecycleAdapter,
    UpdateLifecycleAdapter,
    wire_specialized_providers,
)


def context(token=None):
    return local_owner_context(correlation_id="seam-014", cancellation=token)


class Backend:
    def __init__(self, started=None, release=None, fail=False):
        self.started = started
        self.release = release
        self.fail = fail

    def train(self, request, operation_context):
        if self.started:
            self.started.set()
        while self.release is not None and not self.release.is_set():
            if operation_context.cancellation.cancelled:
                break
            time.sleep(0.001)
        if self.fail:
            raise RuntimeError("startup-independent backend failure")
        return DeploymentResult("backend", request.run_id, "dep-1", "model-1", "a" * 64)


class Activator:
    def activate(self, request, operation_context):
        return ActivationResult("activator", request.activation_id, request.release_id, request.version, request.artifact_digest)


class FailingUpdateAdapter(UpdateLifecycleAdapter):
    def initialize(self, scope):
        raise RuntimeError("activation provider unavailable")


def test_wiring_publishes_all_specialized_capabilities_and_normalizes_identity():
    registry = ScopedProviderRegistry()
    bundle = wire_specialized_providers(
        registry,
        embedding=EmbeddingLifecycleAdapter(lambda request, ctx: ([1.0, 2.0],), provider_id="emb-a"),
        training=TrainingLifecycleAdapter(Backend(), provider_id="train-a"),
        update=UpdateLifecycleAdapter(Activator(), provider_id="update-a"),
    )

    assert set(registry.capabilities()) == {"embedding", "training", "update"}
    assert registry.resolve("emb-a").provider is bundle.registrations[0].provider
    assert registry.resolve("train-a").provider.health().status.value == "healthy"
    assert registry.resolve("update-a").provider.activate(
        ActivationRequest("a", "r", "1", "d" * 64), context()
    ).provider_id == "update-a"
    bundle.close(timeout=0)
    assert registry.providers() == ()


def test_embedding_adapter_converts_vectors_and_enforces_request_shape():
    adapter = EmbeddingLifecycleAdapter(lambda request, ctx: ([1, 2], [3.0, 4.0]))
    registry = ScopedProviderRegistry()
    registry.register(adapter)
    result = adapter.embed(EmbeddingRequest(("one", "two"), "embed-v1"), context())
    assert result.provider_id == "embedding"
    assert result.embeddings == (Embedding((1.0, 2.0), "embed-v1"), Embedding((3.0, 4.0), "embed-v1"))
    with pytest.raises(SpecializedLifecycleError):
        adapter.embed(EmbeddingRequest((), "embed-v1"), context())
    registry.unregister("embedding", timeout=0)


def test_cleanup_waits_for_active_work_and_cancel_reaches_delegate():
    started, release = Event(), Event()
    adapter = TrainingLifecycleAdapter(Backend(started, release))
    registry = ScopedProviderRegistry()
    registry.register(adapter)
    result = []
    worker = Thread(target=lambda: result.append(adapter.train(
        TrainingRequest("run", "base", "rev", "d" * 64), context()
    )))
    worker.start()
    assert started.wait(1)
    assert adapter.cancel()
    assert adapter.cleanup(timeout=0) == CleanupResult("training", False, False, "active operations remain")
    release.set()
    worker.join(1)
    assert adapter.cleanup(timeout=1).quiescent
    assert result


def test_failed_bundle_publication_restores_registry_and_cleans_candidate():
    registry = ScopedProviderRegistry()
    with pytest.raises(Exception):
        wire_specialized_providers(
            registry,
            embedding=EmbeddingLifecycleAdapter(lambda request, ctx: ([1.0],), provider_id="emb"),
            training=TrainingLifecycleAdapter(Backend(), provider_id="training"),
            update=FailingUpdateAdapter(Activator(), provider_id="update"),
        )
    assert registry.providers() == ()
    assert registry.capabilities() == {}
