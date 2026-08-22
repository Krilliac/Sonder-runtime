"""Evaluation lifecycle adapter over the canonical durable event repository."""
from __future__ import annotations

from typing import Mapping

from sonder_runtime.application.ports.session_repository import SessionEvent, SessionRepository


class SessionEvaluationLifecycleRepository:
    """Namespace evaluation events while retaining repository integrity checks."""

    _PREFIX = "evaluation:"

    def __init__(self, repository: SessionRepository) -> None:
        self._repository = repository

    def append(self, proposal_id: str, event_type: str, payload: Mapping[str, object]) -> SessionEvent:
        if not isinstance(proposal_id, str) or not proposal_id.strip():
            raise ValueError("proposal_id must be non-empty")
        if not isinstance(event_type, str) or not event_type.startswith("evaluation."):
            raise ValueError("evaluation event type must use the evaluation namespace")
        return self._repository.append(self._PREFIX + proposal_id, event_type, payload)

    def history(self, proposal_id: str, *, limit: int = 1_000) -> tuple[SessionEvent, ...]:
        return self._repository.read_range(self._PREFIX + proposal_id, limit=limit)


__all__ = ["SessionEvaluationLifecycleRepository"]
