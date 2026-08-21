"""Provider-neutral port for the repository-intelligence application boundary."""
from __future__ import annotations

from typing import Protocol, Sequence

from ..repository_intelligence.index_map import IndexDelta, RankedRepositoryMap
from ..repository_intelligence.lsp_multiroot import (
    MultiRepositoryNavigationResult,
    NavigationProvider,
    RepositoryNavigationPort,
)
from ..repository_intelligence.navigation import ExpansionRequest, NavigationEvidence


class RepositoryIntelligencePort(Protocol):
    """Read-oriented repository intelligence service consumed by interfaces.

    Providers own discovery, parsing, and LSP transport.  This port owns only
    bounded application projections and never performs host I/O.
    """

    @property
    def generation(self) -> int: ...

    def apply(self, delta: IndexDelta) -> int: ...

    def ranked_map(self, query: str = "", *, token_budget: int = 2000) -> RankedRepositoryMap: ...

    def navigate(
        self,
        *,
        symbol: str,
        operation: str,
        lsp_by_root: dict[str, NavigationProvider] | None = None,
    ) -> tuple[MultiRepositoryNavigationResult, ...]: ...

    def expand(
        self,
        evidence: Sequence[NavigationEvidence],
        request: ExpansionRequest,
    ) -> tuple[NavigationEvidence, ...]: ...


__all__ = ["RepositoryIntelligencePort"]
