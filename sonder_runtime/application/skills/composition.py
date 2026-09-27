"""Typed application composition for procedural skill publication.

This module is intentionally small: the publication service remains the
transaction owner, while this facade supplies the memory-policy admission
boundary and injected ports used by a host composition root.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ...domain.memory.wp6_typed import TypedMemory
from ...domain.promotion.measured import PromotionDecision
from ..memory.memory_policy import MemoryPolicy
from ..skill_refresh import SkillRevision
from .procedural_publication import (
    ActiveSkillPort,
    CatalogStorePort,
    DurableLastGoodCatalog,
    HeldOutEvidence,
    PublicationEventPort,
    ProceduralPublicationService,
    PublicationError,
    SkillPublication,
)


@dataclass(frozen=True)
class ProceduralPublicationComposition:
    """The smallest typed graph needed to publish a procedural skill."""

    memory_policy: MemoryPolicy
    catalog: DurableLastGoodCatalog
    active: ActiveSkillPort
    service: ProceduralPublicationService

    def publish(
        self,
        memory: TypedMemory,
        candidate: SkillPublication,
        revision: SkillRevision,
        evidence: HeldOutEvidence,
        decision: PromotionDecision,
        *,
        source_interaction_ids: Sequence[str],
    ) -> SkillPublication:
        """Admit and publish, preserving the service's atomic rollback path."""
        if not memory.is_procedural:
            raise ValueError("procedural publication requires procedural memory")
        admission = self.memory_policy.write(
            "procedural",
            confidence=min(1.0, memory.support_score),
            provenance=source_interaction_ids,
            evidence_count=len(memory.evidence),
            content=memory.content,
        )
        if not admission.allowed:
            raise ValueError("procedural memory admission denied: " + "; ".join(admission.reasons))
        return self.service.publish(
            memory,
            candidate,
            revision,
            evidence,
            decision,
            source_interaction_ids=tuple(source_interaction_ids),
        )

    def rollback(self, skill_id: str) -> SkillPublication:
        """Restore the last-good publication through the guarded service."""
        return self.service.rollback(skill_id)

    def disable(self, skill_id: str, reason: str) -> None:
        """Quarantine a skill through the guarded, persisted service path."""
        self.service.disable(skill_id, reason)

    def enable(self, skill_id: str) -> None:
        """Lift a quarantine through the guarded, persisted service path."""
        self.service.enable(skill_id)


def _restore_active(catalog: DurableLastGoodCatalog, active: ActiveSkillPort) -> None:
    """Re-activate every catalog-active revision after a durable restore."""
    before = active.snapshot()
    try:
        for skill_id, _version in catalog.snapshot().active:
            publication = catalog.current(skill_id)
            if publication is None:
                raise PublicationError("restored catalog points to a missing revision")
            active.activate(publication)
    except BaseException as exc:
        active.restore(before)
        if isinstance(exc, PublicationError):
            raise
        raise PublicationError("restored catalog could not be activated") from exc


def build_procedural_publication_composition(
    *,
    catalog: DurableLastGoodCatalog | None = None,
    active: ActiveSkillPort,
    events: PublicationEventPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    store: CatalogStorePort | None = None,
) -> ProceduralPublicationComposition:
    """Build the typed procedural publication graph from host-owned ports.

    Without ``store`` the catalog defaults to the in-process implementation,
    which is a test/reference adapter only.  With ``store`` the catalog is
    restored from the store's verified ``CatalogSnapshot`` (an empty store
    starts an empty catalog), every catalog-active revision is re-activated
    in ``active``, and every committed publish, rollback, disable, and enable
    is saved to the store inside the guarded transaction.  A snapshot that
    fails its integrity check raises before any port is touched.
    """
    if store is not None and catalog is not None:
        raise ValueError("inject either a catalog or a durable catalog store, not both")
    if store is not None:
        snapshot = store.load()
        resolved_catalog = (
            DurableLastGoodCatalog() if snapshot is None
            else DurableLastGoodCatalog.from_snapshot(snapshot)
        )
        _restore_active(resolved_catalog, active)
    else:
        resolved_catalog = catalog or DurableLastGoodCatalog()
    resolved_policy = memory_policy or MemoryPolicy()
    service = ProceduralPublicationService(resolved_catalog, active, events, store)
    return ProceduralPublicationComposition(
        resolved_policy, resolved_catalog, active, service,
    )


__all__ = [
    "ProceduralPublicationComposition",
    "build_procedural_publication_composition",
]
