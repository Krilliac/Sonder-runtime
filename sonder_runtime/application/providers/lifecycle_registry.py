"""Cross-cutting provider lifecycle registry (WP3 SEAM-015/016).

This service composes the existing provider-neutral lifecycle contracts.  A
provider is initialized against a private staging scope and becomes visible
only after its complete capability surface has been validated.  Overrides are
immutable caller-scope policy, so replacing a provider never mutates the
published provider or leaks across callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from ..ports.specialized_lifecycle import CleanupResult, HealthReport


class ProviderLifecycleError(RuntimeError):
    """Raised when a provider cannot satisfy the lifecycle contract."""


@runtime_checkable
class LifecycleProvider(Protocol):
    """Common provider contract used across capability seams."""

    provider_id: str

    def initialize(self, scope: "ProviderRegistrationScope") -> None: ...

    def health(self) -> HealthReport: ...

    def cleanup(self, timeout: float | None = None) -> CleanupResult: ...


class ProviderRegistrationScope:
    """Single-use private capability staging area."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self._capabilities: dict[str, Any] = {}
        self._open = True

    def register(self, name: str, capability: Any) -> None:
        if not self._open:
            raise ProviderLifecycleError("registration scope is closed")
        if not isinstance(name, str) or not name.strip():
            raise ProviderLifecycleError("capability name must be non-empty")
        if capability is None or name in self._capabilities:
            raise ProviderLifecycleError("invalid or duplicate capability: %r" % name)
        self._capabilities[name] = capability

    def close(self) -> None:
        self._open = False

    def snapshot(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self._capabilities))


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    provider_id: str
    provider: LifecycleProvider
    capabilities: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ScopedProviderOverride:
    scope: str
    provider_id: str
    replacement_id: str

    def __post_init__(self) -> None:
        for field in ("scope", "provider_id", "replacement_id"):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"{field} must be non-empty")
            object.__setattr__(self, field, value)


class ScopedProviderRegistry:
    """Thread-safe registry with atomic publication and scoped resolution."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._providers: dict[str, ProviderRegistration] = {}
        self._capabilities: dict[str, Any] = {}
        self._overrides: dict[tuple[str, str], str] = {}

    def register(self, provider: LifecycleProvider) -> ProviderRegistration:
        provider_id = getattr(provider, "provider_id", "")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProviderLifecycleError("provider_id must be non-empty")
        for method in ("initialize", "health", "cleanup"):
            if not callable(getattr(provider, method, None)):
                raise ProviderLifecycleError(f"provider must define {method}")
        with self._lock:
            if provider_id in self._providers:
                raise ProviderLifecycleError(f"provider {provider_id!r} is already registered")
            scope = ProviderRegistrationScope(provider_id)
            try:
                provider.initialize(scope)
                capabilities = scope.snapshot()
                if not capabilities:
                    raise ProviderLifecycleError("provider must publish a capability")
                conflicts = sorted(set(capabilities) & self._capabilities.keys())
                if conflicts:
                    raise ProviderLifecycleError("capability conflict: " + ", ".join(conflicts))
                registration = ProviderRegistration(provider_id, provider, capabilities)
                self._providers[provider_id] = registration
                self._capabilities.update(capabilities)
                return registration
            except BaseException as exc:
                try:
                    provider.cleanup(timeout=0)
                except BaseException as cleanup_exc:
                    raise ProviderLifecycleError("provider initialization and cleanup failed") from cleanup_exc
                if isinstance(exc, ProviderLifecycleError):
                    raise
                raise ProviderLifecycleError(f"provider {provider_id!r} failed to initialize") from exc
            finally:
                scope.close()

    def publish_override(self, override: ScopedProviderOverride) -> None:
        """Atomically publish one caller-scope replacement rule."""
        with self._lock:
            if override.provider_id not in self._providers:
                raise ProviderLifecycleError("unknown base provider")
            if override.replacement_id not in self._providers:
                raise ProviderLifecycleError("unknown replacement provider")
            key = (override.scope, override.provider_id)
            if key in self._overrides:
                raise ProviderLifecycleError("override already published")
            self._overrides[key] = override.replacement_id

    def resolve(self, provider_id: str, scopes: Sequence[str] | None = None) -> ProviderRegistration:
        with self._lock:
            resolved = provider_id
            for scope in scopes or ():
                resolved = self._overrides.get((scope, provider_id), provider_id)
                if resolved != provider_id:
                    break
            try:
                return self._providers[resolved]
            except KeyError as exc:
                raise ProviderLifecycleError(f"unknown provider {resolved!r}") from exc

    def health(self, provider_id: str, scopes: Sequence[str] | None = None) -> HealthReport:
        return self.resolve(provider_id, scopes).provider.health()

    def unregister(self, provider_id: str, timeout: float | None = None) -> ProviderRegistration:
        with self._lock:
            registration = self._providers.get(provider_id)
            if registration is None:
                raise ProviderLifecycleError(f"unknown provider {provider_id!r}")
            result = registration.provider.cleanup(timeout=timeout)
            if not isinstance(result, CleanupResult) or not result.quiescent or not result.resources_released:
                raise ProviderLifecycleError("provider cleanup did not reach a quiescent released state")
            del self._providers[provider_id]
            for name in registration.capabilities:
                self._capabilities.pop(name, None)
            self._overrides = {
                key: value for key, value in self._overrides.items()
                if key[0] and key[1] != provider_id and value != provider_id
            }
            return registration

    def providers(self) -> tuple[ProviderRegistration, ...]:
        with self._lock:
            return tuple(self._providers.values())

    def capabilities(self) -> Mapping[str, Any]:
        with self._lock:
            return MappingProxyType(dict(self._capabilities))
