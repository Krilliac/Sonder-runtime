"""Authenticated HTTP discovery facade for the local A2A Agent Card."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)

from sonder_runtime.application.protocol.a2a import A2AAgentCard, card_from_registrations


@dataclass(frozen=True)
class A2AAgentCardRoute:
    path: str
    requires_auth: bool = True
    media_type: str = "application/json"

    def render(self, card: A2AAgentCard) -> tuple[int, dict]:
        return 200, {"agentCard": card.to_dict(), "digest": card.digest}


class A2AAgentCardFacade:
    """Classify and render discovery only; no remote calls are made."""

    PATH = "/.well-known/agent-card.json"

    def route(self, path: str) -> A2AAgentCardRoute | None:
        normalized = str(path or "").split("?", 1)[0].rstrip("/") or "/"
        matched = normalized == self.PATH
        if matched:
            logger.debug("A2AAgentCardFacade.route: matched agent-card discovery path")
        return A2AAgentCardRoute(self.PATH) if matched else None

    def card(
        self,
        registrations: Iterable[object],
        *,
        base_url: str,
        version: str = "1",
    ) -> A2AAgentCard:
        logger.info(f"Building A2A agent card, base_url={base_url!r}, version={version!r}")
        logger.debug(f"A2AAgentCardFacade.card: base_url={base_url!r}, version={version!r}")
        return card_from_registrations(
            "sonder-runtime",
            "Sonder Runtime agent host",
            base_url.rstrip("/") + "/a2a",
            registrations,
            version=version,
        )


__all__ = ["A2AAgentCardFacade", "A2AAgentCardRoute"]
