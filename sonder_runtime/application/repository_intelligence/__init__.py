"""Pure application services for repository intelligence (SPEC WP4).

The facade is exposed lazily so a port importing the typed index modules does
not re-enter this package through the facade during package initialization.
"""

from __future__ import annotations


def __getattr__(name: str):
    if name == "RepositoryIntelligenceFacade":
        from .facade import RepositoryIntelligenceFacade

        return RepositoryIntelligenceFacade
    raise AttributeError(name)

from .index_map import FileEvidence, IndexDelta, MapEntry, RankedRepositoryMap, RepositoryIndex, SymbolRecord, digest_bytes
from .lsp_multiroot import (
    FileRevisionEvidence,
    LiveLspProvider,
    LspCapabilities,
    LspNegotiator,
    LspSession,
    LspTransport,
    MultiRepositoryNavigationResult,
    MultiRepositoryNavigator,
    MultiRootReadContext,
    NavigationBackend,
    NavigationProvider,
    RepositoryNavigationPort,
    RepositoryRoot,
    authorize_write,
    bind_navigation_evidence,
    open_live_lsp,
)

__all__ = [
    "FileEvidence", "IndexDelta", "MapEntry", "RankedRepositoryMap", "RepositoryIndex", "SymbolRecord", "digest_bytes",
    "RepositoryIntelligenceFacade",
    "FileRevisionEvidence", "LiveLspProvider", "LspCapabilities", "LspNegotiator", "LspSession", "LspTransport",
    "MultiRepositoryNavigationResult", "MultiRepositoryNavigator", "MultiRootReadContext",
    "NavigationBackend", "NavigationProvider", "RepositoryNavigationPort", "RepositoryRoot",
    "authorize_write", "bind_navigation_evidence", "open_live_lsp",
]
