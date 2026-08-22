"""WP3 SEAM-016: provider lifecycle and atomic registration tests."""
from __future__ import annotations

import pytest

from sonder_runtime.application.capabilities import (
    CapabilityRegistry,
    ProviderLifecycleError,
)
from sonder_runtime.application.ports.specialized_lifecycle import (
    CleanupResult,
    HealthReport,
    HealthStatus,
)
from sonder_runtime.application.providers.lifecycle_registry import (
    ProviderLifecycleError as ScopedProviderLifecycleError,
    ScopedProviderRegistry,
)


class FakeProvider:
    def __init__(self, provider_id="alpha", *, fail=False, names=("read",)):
        self.provider_id = provider_id
        self.fail = fail
        self.names = names
        self.initialized = False
        self.shutdown_count = 0
        self.scope = None

    def initialize(self, scope):
        self.scope = scope
        for name in self.names:
            scope.register(name, object())
        self.initialized = True
        if self.fail:
            raise RuntimeError("startup failed")

    def shutdown(self):
        self.shutdown_count += 1


def test_success_publishes_all_capabilities_as_one_provider():
    registry = CapabilityRegistry()
    provider = FakeProvider(names=("read", "write"))

    published = registry.register(provider)

    assert published.provider is provider
    assert set(published.capabilities) == {"read", "write"}
    assert registry.get("read") is published.capabilities["read"]
    assert registry.provider("alpha") is published
    assert provider.shutdown_count == 0


def test_failed_initialization_rolls_back_every_staged_registration():
    registry = CapabilityRegistry()
    provider = FakeProvider(fail=True, names=("first", "second"))

    with pytest.raises(ProviderLifecycleError, match="failed to initialize"):
        registry.register(provider)

    assert registry.capabilities() == {}
    assert registry.providers() == ()
    assert provider.shutdown_count == 1
    with pytest.raises(ProviderLifecycleError, match="scope is closed"):
        provider.scope.register("late", object())


def test_conflict_does_not_disturb_existing_provider_and_cleans_candidate():
    registry = CapabilityRegistry()
    existing = FakeProvider()
    candidate = FakeProvider("beta", names=("read", "write"))
    registry.register(existing)

    with pytest.raises(ProviderLifecycleError, match="already registered"):
        registry.register(candidate)

    assert registry.provider("alpha").provider is existing
    assert registry.get("write") is None
    assert candidate.shutdown_count == 1


def test_empty_provider_is_rejected_and_cleaned_up():
    registry = CapabilityRegistry()
    provider = FakeProvider(names=())

    with pytest.raises(ProviderLifecycleError, match="no capabilities"):
        registry.register(provider)

    assert registry.providers() == ()
    assert provider.shutdown_count == 1


def test_published_snapshot_cannot_be_mutated_and_unregisters_cleanly():
    registry = CapabilityRegistry()
    provider = FakeProvider(names=("read",))
    published = registry.register(provider)

    with pytest.raises(TypeError):
        published.capabilities["other"] = object()  # type: ignore[index]
    registry.unregister("alpha")

    assert registry.get("read") is None
    assert registry.providers() == ()
    assert provider.shutdown_count == 1


def test_failed_initialization_rejects_non_quiescent_cleanup():
    class LeakyProvider:
        provider_id = "leaky"

        def initialize(self, scope):
            scope.register("read", object())
            raise RuntimeError("startup failed")

        def health(self):
            return HealthReport(self.provider_id, HealthStatus.UNHEALTHY)

        def cleanup(self, timeout=None):
            return CleanupResult(self.provider_id, False, False, "active work remains")

    registry = ScopedProviderRegistry()
    provider = LeakyProvider()

    with pytest.raises(ScopedProviderLifecycleError, match="initialization and cleanup failed"):
        registry.register(provider)

    assert registry.providers() == ()
    assert registry.capabilities() == {}
