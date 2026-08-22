"""Persistence adapter factory for canonical session checkpoint/privacy use."""
from __future__ import annotations

from ...application.ports.session_repository import SessionRepository
from ...application.session.checkpoint_privacy import SessionCheckpointPrivacyService

def build_session_checkpoint_privacy_adapter(repository: SessionRepository, *, max_scan: int = 10_000) -> SessionCheckpointPrivacyService:
    return SessionCheckpointPrivacyService(repository, max_scan=max_scan)

__all__ = ["build_session_checkpoint_privacy_adapter"]
