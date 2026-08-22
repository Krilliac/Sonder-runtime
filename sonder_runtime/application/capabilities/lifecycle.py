"""Atomic lifecycle for application capability providers (WP3 SEAM-016).

Providers are initialized against a private registration scope.  The scope
and its provider are not visible through the registry until initialization,
validation, and publication have all succeeded.  A failed initialization is
cleaned up before the exception is returned to the caller.

This module is deliberately provider- and transport-neutral.  It owns the
application capability boundary only; gateways and tool registries are not
implemented here.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Mapping, Protocol


class ProviderLifecycleError(RuntimeError):
    """Raised when a provider cannot be initialized or registered."""


class Provider(Protocol):
    """Shape required by :class:`CapabilityRegistry`.

    ``initialize`` must register every capability needed by the provider.
    ``shutdown`` must release resources acquired during initialization and is
    called for failed initialization as well as explicit removal.
    """

    provider_id: str

    def initialize(self, scope: "RegistrationScope") -> None: ...

    def shutdown(self) -> None: ...


@dataclass(frozen=True)
class ProviderRegistration:
    """Immutable view of one published provider."""

    provider_id: str
    provider: Provider
    capabilities: Mapping[str, Any]


class RegistrationScope:
    """Private capability staging area supplied during provider startup.

    The scope is single-use and cannot be retained for registrations after
    ``initialize`` returns.  It intentionally has no lookup or remove API:
    initialization either stages a complete provider surface or publishes
    nothing.
    """

    def __init__(self, provider_id: str) -> None:
        self._provider_id = provider_id
        self._capabilities: dict[str, Any] = {}
        self._open = True

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def register(self, name: str, capability: Any) -> None:
        """Stage one non-null capability under a unique name."""
        if not self._open:
            raise ProviderLifecycleError("registration scope is closed")
        if not isinstance(name, str) or not name.strip():
            raise ProviderLifecycleError("capability name must be non-empty")
        if capability is None:
            raise ProviderLifecycleError(
                "capability %r has no usable implementation" % name
            )
        if name in self._capabilities:
            raise ProviderLifecycleError(
                "provider %r registered capability %r more than once"
                % (self._provider_id, name)
            )
        self._capabilities[name] = capability

    def close(self) -> None:
        self._open = False

    def _snapshot(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self._capabilities))


class CapabilityRegistry:
    """Thread-safe registry with atomic provider publication.

    Public reads use the currently published immutable snapshot and therefore
    never observe a provider halfway through initialization.  Initialization
    and publication are serialized; provider callbacks must not call back
    into this registry while they initialize.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._providers: dict[str, ProviderRegistration] = {}
        self._capabilities: dict[str, Any] = {}

    def register(self, provider: Provider) -> ProviderRegistration:
        """Initialize and atomically publish ``provider``.

        Any exception from initialization, validation, or publication causes
        the private scope to be discarded and ``shutdown`` to be attempted.
        The registry remains exactly as it was before this call.
        """
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProviderLifecycleError("provider_id must be non-empty")
        if not callable(getattr(provider, "initialize", None)):
            raise ProviderLifecycleError("provider must define initialize")
        if not callable(getattr(provider, "shutdown", None)):
            raise ProviderLifecycleError("provider must define shutdown")

        with self._lock:
            if provider_id in self._providers:
                raise ProviderLifecycleError(
                    "provider %r is already registered" % provider_id
                )

            scope = RegistrationScope(provider_id)
            try:
                provider.initialize(scope)
                capabilities = scope._snapshot()
                if not capabilities:
                    raise ProviderLifecycleError(
                        "provider %r registered no capabilities" % provider_id
                    )
                conflicts = sorted(
                    name for name in capabilities if name in self._capabilities
                )
                if conflicts:
                    raise ProviderLifecycleError(
                        "capabilities already registered: %s"
                        % ", ".join(conflicts)
                    )

                registration = ProviderRegistration(
                    provider_id=provider_id,
                    provider=provider,
                    capabilities=capabilities,
                )
                # The two maps are updated while readers are excluded.  No
                # public read can see either half of this publication.
                self._providers[provider_id] = registration
                self._capabilities.update(capabilities)
                return registration
            except BaseException as exc:
                try:
                    provider.shutdown()
                except BaseException as cleanup_exc:
                    raise ProviderLifecycleError(
                        "provider %r failed to initialize and cleanup failed"
                        % provider_id
                    ) from cleanup_exc
                if isinstance(exc, ProviderLifecycleError):
                    raise
                raise ProviderLifecycleError(
                    "provider %r failed to initialize" % provider_id
                ) from exc
            finally:
                scope.close()

    def get(self, capability_name: str) -> Any | None:
        """Return a published capability, or ``None`` when absent."""
        with self._lock:
            return self._capabilities.get(capability_name)

    def provider(self, provider_id: str) -> ProviderRegistration | None:
        """Return a published provider view, or ``None`` when absent."""
        with self._lock:
            return self._providers.get(provider_id)

    def providers(self) -> tuple[ProviderRegistration, ...]:
        """Return a stable snapshot of published providers."""
        with self._lock:
            return tuple(self._providers.values())

    def capabilities(self) -> Mapping[str, Any]:
        """Return an immutable snapshot of published capabilities."""
        with self._lock:
            return MappingProxyType(dict(self._capabilities))

    def unregister(self, provider_id: str) -> ProviderRegistration:
        """Atomically remove a provider, then release its resources."""
        with self._lock:
            registration = self._providers.pop(provider_id, None)
            if registration is None:
                raise ProviderLifecycleError(
                    "provider %r is not registered" % provider_id
                )
            for name in registration.capabilities:
                self._capabilities.pop(name, None)
            try:
                registration.provider.shutdown()
            except BaseException as exc:
                raise ProviderLifecycleError(
                    "provider %r failed to shut down" % provider_id
                ) from exc
            return registration
