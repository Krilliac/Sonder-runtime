from dataclasses import dataclass

import pytest

from sonder_runtime.application.ports.specialized_lifecycle import (
    CleanupResult,
    HealthReport,
    HealthStatus,
)
from sonder_runtime.application.providers.lifecycle_registry import (
    ProviderLifecycleError,
    ScopedProviderOverride,
    ScopedProviderRegistry,
)


@dataclass
class FakeProvider:
    provider_id: str
    capability: str
    fail: bool = False
    cleaned: int = 0

    def initialize(self, scope):
        if self.fail:
            raise RuntimeError("boom")
        scope.register(self.capability, object())

    def health(self):
        return HealthReport(self.provider_id, HealthStatus.HEALTHY)

    def cleanup(self, timeout=None):
        self.cleaned += 1
        return CleanupResult(self.provider_id, True, True)


def test_publish_is_atomic_and_failed_initialization_is_cleaned_up():
    registry = ScopedProviderRegistry()
    good = FakeProvider("base", "chat")
    registry.register(good)
    bad = FakeProvider("bad", "broken", fail=True)
    with pytest.raises(ProviderLifecycleError):
        registry.register(bad)
    assert bad.cleaned == 1
    assert [item.provider_id for item in registry.providers()] == ["base"]
    assert set(registry.capabilities()) == {"chat"}


def test_scoped_override_resolves_without_mutating_global_base():
    registry = ScopedProviderRegistry()
    base = FakeProvider("base", "chat")
    local = FakeProvider("local", "chat-local")
    registry.register(base)
    registry.register(local)
    registry.publish_override(ScopedProviderOverride("agent-1", "base", "local"))
    assert registry.resolve("base").provider_id == "base"
    assert registry.resolve("base", ("agent-1",)).provider_id == "local"
    assert registry.health("base", ("agent-1",)).status is HealthStatus.HEALTHY


def test_cleanup_must_be_quiescent_before_unpublish():
    class Busy(FakeProvider):
        def cleanup(self, timeout=None):
            self.cleaned += 1
            return CleanupResult(self.provider_id, False, False)

    registry = ScopedProviderRegistry()
    busy = Busy("busy", "stream")
    registry.register(busy)
    with pytest.raises(ProviderLifecycleError):
        registry.unregister("busy")
    assert registry.resolve("busy").provider_id == "busy"


def test_override_targets_are_validated_and_removed_with_provider():
    registry = ScopedProviderRegistry()
    registry.register(FakeProvider("base", "base-cap"))
    registry.register(FakeProvider("replacement", "replacement-cap"))
    registry.publish_override(ScopedProviderOverride("scope", "base", "replacement"))
    with pytest.raises(ProviderLifecycleError):
        registry.publish_override(ScopedProviderOverride("scope", "base", "replacement"))
    registry.unregister("replacement")
    assert registry.resolve("base", ("scope",)).provider_id == "base"


def test_cancellation_resolves_the_scoped_provider_and_returns_activity_state():
    class Cancellable(FakeProvider):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.reasons = []

        def cancel(self, *, reason="cancellation requested"):
            self.reasons.append(reason)
            return True

    registry = ScopedProviderRegistry()
    base = Cancellable("base", "chat")
    replacement = Cancellable("replacement", "chat-replacement")
    registry.register(base)
    registry.register(replacement)
    registry.publish_override(ScopedProviderOverride("scope", "base", "replacement"))

    assert registry.cancel("base", reason="stop", scopes=("scope",)) is True
    assert base.reasons == []
    assert replacement.reasons == ["stop"]
