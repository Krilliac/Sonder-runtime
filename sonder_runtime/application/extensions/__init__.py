"""Application services for extension admission and quarantine."""

from .quarantine import QuarantineDecision, QuarantineReason, QuarantineRegistry

__all__ = ["QuarantineDecision", "QuarantineReason", "QuarantineRegistry"]
