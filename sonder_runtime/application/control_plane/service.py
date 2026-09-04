"""Provider-backed assembly of the operator control-plane snapshot.

The service deliberately owns no runtime state and performs no I/O.  Callers
inject read-only section providers from the application services that own the
underlying data.  A missing or failing provider aborts the complete snapshot;
silently returning a partial operator view would be unsafe.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .snapshot import (
    CONTROL_PLANE_SECTIONS,
    ControlPlaneSnapshot,
    SnapshotValidationError,
)


logger = logging.getLogger(__name__)

SectionProvider = Callable[[], Iterable[Mapping[str, Any]]]


class ControlPlaneProviderError(RuntimeError):
    """Raised when a complete control-plane snapshot cannot be assembled."""


class ControlPlaneSnapshotService:
    """Assemble a deterministic snapshot from all required section ports."""

    def __init__(self, providers: Mapping[str, SectionProvider]) -> None:
        logger.debug(f"ControlPlaneSnapshotService.__init__: provider_sections={sorted(providers.keys()) if isinstance(providers, Mapping) else 'invalid'}")
        if not isinstance(providers, Mapping):
            raise ControlPlaneProviderError("providers must be a mapping")
        unknown = set(providers) - set(CONTROL_PLANE_SECTIONS)
        missing = set(CONTROL_PLANE_SECTIONS) - set(providers)
        if unknown:
            raise ControlPlaneProviderError(f"unknown section providers: {sorted(unknown)}")
        if missing:
            raise ControlPlaneProviderError(f"missing section providers: {sorted(missing)}")
        if any(not callable(provider) for provider in providers.values()):
            raise ControlPlaneProviderError("section providers must be callable")
        self._providers = dict(providers)
        logger.info(f"control-plane snapshot service initialized: sections={sorted(providers.keys())}")

    def snapshot(self, *, captured_at: str, revision: int = 0) -> ControlPlaneSnapshot:
        logger.debug(f"ControlPlaneSnapshotService.snapshot: captured_at={captured_at!r}, revision={revision}")
        sections: dict[str, Iterable[Mapping[str, Any]]] = {}
        for name in CONTROL_PLANE_SECTIONS:
            try:
                records = self._providers[name]()
                if records is None or isinstance(records, (str, bytes, Mapping)):
                    raise TypeError("provider must return an iterable of mappings")
                sections[name] = tuple(records)
            except Exception as exc:
                logger.error(f"control-plane section provider failed: section={name!r}, error={type(exc).__name__}", exc_info=True)
                logger.warning(f"control-plane section provider failed: section={name!r}, error={type(exc).__name__}")
                logger.debug(f"ControlPlaneSnapshotService.snapshot: section {name!r} provider failed with {type(exc).__name__}")
                if isinstance(exc, SnapshotValidationError):
                    raise ControlPlaneProviderError(
                        f"section {name} returned invalid records"
                    ) from exc
                raise ControlPlaneProviderError(
                    f"section {name} provider failed: {type(exc).__name__}"
                ) from exc
        try:
            snapshot = ControlPlaneSnapshot.build(
                captured_at=captured_at,
                revision=revision,
                **sections,
            )
        except SnapshotValidationError as exc:
            logger.critical(f"control-plane snapshot validation failed after assembly: revision={revision}, captured_at={captured_at!r} — operator visibility is lost", exc_info=True)
            raise ControlPlaneProviderError("control-plane snapshot validation failed") from exc
        logger.info(f"control-plane snapshot assembled: revision={revision}, sections={len(sections)}, captured_at={captured_at!r}")
        return snapshot
