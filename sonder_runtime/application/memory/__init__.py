"""Memory and learning application services (SPEC-5 WP4)."""

from .facade import MemoryLearningFacade
from .receipt_bound_fact_promotion import (
    FactPromotionRequest,
    FactPromotionResult,
    ReceiptBoundFactPromotion,
)
from .receipt_observation import (
    ReceiptObservationProducer,
    VerifierReceipt,
    VerifiedSubjectFactPromotion,
)
from .replication import (
    MemoryReplicationCoordinator,
    MemoryReplicationOutcome,
    MemoryReplicationSink,
    SQLiteMemoryReplicationSink,
)

__all__ = [
    "MemoryLearningFacade",
    "MemoryReplicationCoordinator",
    "MemoryReplicationOutcome",
    "MemoryReplicationSink",
    "SQLiteMemoryReplicationSink",
    "ReceiptObservationProducer",
    "VerifierReceipt",
    "VerifiedSubjectFactPromotion",
    "FactPromotionRequest",
    "FactPromotionResult",
    "ReceiptBoundFactPromotion",
]
