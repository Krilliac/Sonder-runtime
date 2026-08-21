"""LOOP-006 local execution-provider cleanup orchestration.

The general provider registry contains capability providers that are not
necessarily process owners.  This boundary therefore owns an explicit set of
local execution registrations.  Every registration receives the same
cancel/cleanup request, while process-tree cleanup is attempted only when the
registration supplies a typed :class:`ProcessTreeCleanupRequest` factory.

No receipt in this module upgrades an unsupported or failed operation to a
successful cleanup.  The aggregate is complete only when provider quiescence,
resource release, and (when applicable) process-tree cleanup are all proven.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from ..jobs.durable_registry import (
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from ..ports.specialized_lifecycle import CleanupResult


class LocalExecutionProvider(Protocol):
    """Minimum lifecycle required of a locally registered execution owner."""

    provider_id: str

    def cancel(self, *, reason: str = "cancellation requested") -> bool: ...

    def cleanup(self, timeout: float | None = None) -> CleanupResult: ...


ProcessCleanupRequestFactory = Callable[
    [str], ProcessTreeCleanupRequest | None
]


class ProcessTreeCleanupSupervisor(Protocol):
    """Injected platform port implemented by the process supervisor adapter."""

    def cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt: ...


@dataclass(frozen=True, slots=True)
class LocalProviderCleanupReceipt:
    """Truthful result for one registered local execution provider."""

    provider_id: str
    cancellation_requested: bool
    provider_cleanup: CleanupResult
    process_cleanup: ProcessTreeCleanupReceipt

    @property
    def complete(self) -> bool:
        return (
            self.cancellation_requested
            and self.provider_cleanup.quiescent
            and self.provider_cleanup.resources_released
            and self.process_cleanup.complete
        )


class LocalExecutionProviderRegistry:
    """Stable registry and cleanup fan-out for local execution providers."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._registrations: dict[str, tuple[LocalExecutionProvider, ProcessCleanupRequestFactory | None]] = {}

    def register(
        self,
        provider: LocalExecutionProvider,
        *,
        process_request: ProcessCleanupRequestFactory | None = None,
    ) -> None:
        provider_id = getattr(provider, "provider_id", "")
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        if not callable(getattr(provider, "cancel", None)):
            raise TypeError("local execution provider must define cancel")
        if not callable(getattr(provider, "cleanup", None)):
            raise TypeError("local execution provider must define cleanup")
        if process_request is not None and not callable(process_request):
            raise TypeError("process_request must be callable")
        with self._lock:
            if provider_id in self._registrations:
                raise ValueError(f"provider {provider_id!r} is already registered")
            self._registrations[provider_id] = (provider, process_request)

    def providers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._registrations)

    def cancel_and_cleanup(
        self,
        supervisor: ProcessTreeCleanupSupervisor,
        *,
        reason: str = "cancellation requested",
        timeout: float | None = None,
    ) -> tuple[LocalProviderCleanupReceipt, ...]:
        """Cancel and clean every registration, retaining incomplete truth."""
        if not callable(getattr(supervisor, "cleanup", None)):
            raise TypeError("supervisor must provide typed cleanup")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("cancellation reason is required")
        with self._lock:
            registrations = tuple(self._registrations.values())

        receipts: list[LocalProviderCleanupReceipt] = []
        for provider, request_factory in registrations:
            provider_id = str(getattr(provider, "provider_id", ""))
            try:
                cancellation_requested = bool(provider.cancel(reason=reason))
            except Exception as exc:
                cancellation_requested = False
                cancel_detail = f"cancel failed: {type(exc).__name__}"
            else:
                cancel_detail = "cancellation requested" if cancellation_requested else "no active work"

            try:
                provider_cleanup = provider.cleanup(timeout)
                if not isinstance(provider_cleanup, CleanupResult):
                    raise TypeError("provider cleanup must return CleanupResult")
            except Exception as exc:
                provider_cleanup = CleanupResult(
                    provider_id, False, False,
                    f"cleanup failed: {type(exc).__name__}; {cancel_detail}",
                )

            process_cleanup = self._process_cleanup(
                provider_id, request_factory, supervisor, reason
            )
            receipts.append(LocalProviderCleanupReceipt(
                provider_id,
                cancellation_requested,
                provider_cleanup,
                process_cleanup,
            ))
        return tuple(receipts)

    @staticmethod
    def _process_cleanup(
        provider_id: str,
        request_factory: ProcessCleanupRequestFactory | None,
        supervisor: ProcessTreeCleanupSupervisor,
        reason: str,
    ) -> ProcessTreeCleanupReceipt:
        if request_factory is None:
            return ProcessTreeCleanupReceipt(
                provider_id,
                False,
                complete=False,
                detail="unsupported local process cleanup; typed request is not registered",
            )
        try:
            request = request_factory(reason)
            if not isinstance(request, ProcessTreeCleanupRequest):
                raise TypeError("process request factory must return ProcessTreeCleanupRequest")
            if request.job_id != provider_id:
                raise ValueError("process cleanup request identity does not match provider")
            receipt = supervisor.cleanup(request)
            if not isinstance(receipt, ProcessTreeCleanupReceipt):
                raise TypeError("supervisor must return ProcessTreeCleanupReceipt")
            return receipt
        except Exception as exc:
            return ProcessTreeCleanupReceipt(
                provider_id,
                False,
                complete=False,
                detail=f"process cleanup failed: {type(exc).__name__}",
            )


__all__ = [
    "LocalExecutionProvider",
    "LocalExecutionProviderRegistry",
    "LocalProviderCleanupReceipt",
    "ProcessTreeCleanupSupervisor",
    "ProcessCleanupRequestFactory",
]
