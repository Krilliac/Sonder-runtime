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
    DurableLastGoodCatalog,
    HeldOutEvidence,
    PublicationEventPort,
    ProceduralPublicationService,
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


def build_procedural_publication_composition(
    *,
    catalog: DurableLastGoodCatalog | None = None,
    active: ActiveSkillPort,
    events: PublicationEventPort | None = None,
    memory_policy: MemoryPolicy | None = None,
) -> ProceduralPublicationComposition:
    """Build the typed procedural publication graph from host-owned ports.

    The catalog defaults to the existing in-process implementation only as a
    test/reference adapter.  Hosts that need durability must inject a catalog
    restored from their verified ``CatalogSnapshot`` persistence seam.
    """
    resolved_catalog = catalog or DurableLastGoodCatalog()
    resolved_policy = memory_policy or MemoryPolicy()
    service = ProceduralPublicationService(resolved_catalog, active, events)
    return ProceduralPublicationComposition(
        resolved_policy, resolved_catalog, active, service,
    )


__all__ = [
    "ProceduralPublicationComposition",
    "build_procedural_publication_composition",
]
