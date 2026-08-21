"""Application facade composing the typed repository-intelligence contracts."""
from __future__ import annotations

from typing import Iterable, Sequence

from ..ports.repository_intelligence import RepositoryIntelligencePort
from .index_map import IndexDelta, RankedRepositoryMap, RepositoryIndex
from .lsp_multiroot import (
    MultiRepositoryNavigationResult,
    MultiRepositoryNavigator,
    NavigationProvider,
    RepositoryNavigationPort,
)
from .navigation import ExpansionRequest, NavigationEvidence, expand


class RepositoryIntelligenceFacade(RepositoryIntelligencePort):
    """Bounded, provider-neutral entry point for repository intelligence.

    The facade is application-owned and in-memory.  A provider supplies typed
    index deltas, repository ports, and (optionally) already-open LSP providers.
    No filesystem, subprocess, network, or language-server discovery occurs
    here.  Callers retain responsibility for closing any supplied LSP provider.
    """

    def __init__(
        self,
        records=(),
        *,
        repositories: Iterable[RepositoryNavigationPort] = (),
        max_navigation_results: int = 100,
    ) -> None:
        self._index = RepositoryIndex(records)
        repository_ports = tuple(repositories)
        self._navigator = (
            MultiRepositoryNavigator(repository_ports, max_results=max_navigation_results)
            if repository_ports else None
        )

    @property
    def generation(self) -> int:
        return self._index.generation

    def apply(self, delta: IndexDelta) -> int:
        return self._index.apply(delta)

    def ranked_map(self, query: str = "", *, token_budget: int = 2000) -> RankedRepositoryMap:
        return self._index.ranked_map(query, token_budget=token_budget)

    def navigate(
        self,
        *,
        symbol: str,
        operation: str,
        lsp_by_root: dict[str, NavigationProvider] | None = None,
    ) -> tuple[MultiRepositoryNavigationResult, ...]:
        if self._navigator is None:
            raise LookupError("no repository navigation providers configured")
        return self._navigator.query(symbol=symbol, operation=operation, lsp_by_root=lsp_by_root)

    def expand(
        self,
        evidence: Sequence[NavigationEvidence],
        request: ExpansionRequest,
    ) -> tuple[NavigationEvidence, ...]:
        return expand(tuple(evidence), request)


__all__ = ["RepositoryIntelligenceFacade"]
