"""Optional LSP navigation and multi-root repository contracts.

The module contains only application-level contracts.  An LSP client, index,
or lexical scanner is injected by an adapter; this module never reads files or
contacts a language server.  Every returned location carries the repository
root, file digest, and independent Git revision used to produce it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Protocol, Sequence

from .navigation import NavigationEvidence

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class RepositoryRoot:
    """A read-visible root with its own history and optional write owner."""

    root_id: str
    display_name: str
    git_revision: str
    write_owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _text(self.root_id, "root_id"))
        object.__setattr__(self, "display_name", _text(self.display_name, "display_name"))
        object.__setattr__(self, "git_revision", _text(self.git_revision, "git_revision"))
        if self.write_owner is not None:
            object.__setattr__(self, "write_owner", _text(self.write_owner, "write_owner"))


@dataclass(frozen=True, slots=True)
class MultiRootReadContext:
    """Explicitly bounded read context; roots retain independent revisions."""

    roots: tuple[RepositoryRoot, ...]
    write_root_id: str | None = None

    def __post_init__(self) -> None:
        roots = tuple(self.roots)
        if not roots or any(not isinstance(root, RepositoryRoot) for root in roots):
            raise ValueError("at least one RepositoryRoot is required")
        if len({root.root_id for root in roots}) != len(roots):
            raise ValueError("root_id values must be unique")
        if self.write_root_id is not None and self.write_root_id not in {root.root_id for root in roots}:
            raise ValueError("write_root_id must identify a visible root")
        object.__setattr__(self, "roots", roots)

    def root(self, root_id: str) -> RepositoryRoot:
        for root in self.roots:
            if root.root_id == root_id:
                return root
        raise KeyError(root_id)

    def can_write(self, root_id: str, actor: str) -> bool:
        """Return true only for the explicitly selected root and owner."""
        if self.write_root_id != root_id:
            return False
        return self.root(root_id).write_owner == actor


@dataclass(frozen=True, slots=True)
class FileRevisionEvidence:
    root_id: str
    path: str
    sha256: str
    git_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_id", _text(self.root_id, "root_id"))
        object.__setattr__(self, "path", _text(self.path, "path").replace("\\", "/"))
        digest = _text(self.sha256, "sha256").lower()
        if not _SHA256.fullmatch(digest):
            raise ValueError("sha256 must be a lowercase hexadecimal SHA-256 digest")
        object.__setattr__(self, "sha256", digest)
        object.__setattr__(self, "git_revision", _text(self.git_revision, "git_revision"))

    @classmethod
    def from_bytes(cls, root_id: str, path: str, content: bytes, git_revision: str) -> "FileRevisionEvidence":
        return cls(root_id, path, hashlib.sha256(content).hexdigest(), git_revision)


@dataclass(frozen=True, slots=True)
class LspCapabilities:
    server_id: str
    root_id: str
    languages: frozenset[str] = frozenset()
    operations: frozenset[str] = frozenset()
    revision: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "server_id", _text(self.server_id, "server_id"))
        object.__setattr__(self, "root_id", _text(self.root_id, "root_id"))
        object.__setattr__(self, "languages", frozenset(_text(v, "language").casefold() for v in self.languages))
        object.__setattr__(self, "operations", frozenset(_text(v, "operation") for v in self.operations))
        object.__setattr__(self, "revision", self.revision.strip())


@dataclass(frozen=True, slots=True)
class NavigationBackend:
    mode: str
    root_id: str
    reason: str
    server_id: str | None = None


class NavigationProvider(Protocol):
    def query(self, root_id: str, symbol: str, operation: str) -> Sequence[NavigationEvidence]: ...


class LspSession(Protocol):
    """One adapter-owned live language-server session."""

    def query(
        self, *, root_id: str, symbol: str, operation: str, max_results: int
    ) -> Sequence[NavigationEvidence]: ...

    def close(self) -> None: ...


class LspTransport(Protocol):
    """Provider-neutral transport for an initialized LSP client."""

    def open(
        self,
        *,
        root_id: str,
        language: str,
        operations: frozenset[str],
        max_results: int,
    ) -> LspSession: ...


class RepositoryNavigationPort(Protocol):
    """Read-only repository adapter consumed by multi-root navigation."""

    @property
    def root(self) -> RepositoryRoot: ...

    def language_for(self, symbol: str) -> str: ...

    def indexed_provider(self) -> NavigationProvider | None: ...

    def lexical_provider(self) -> NavigationProvider | None: ...


class LiveLspProvider:
    """Bounded navigation provider backed by one live, injected LSP session."""

    def __init__(self, session: LspSession, *, max_results: int = 100) -> None:
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10_000:
            raise ValueError("max_results must be between 1 and 10000")
        self._session = session
        self._max_results = max_results
        self._closed = False

    def query(self, root_id: str, symbol: str, operation: str) -> tuple[NavigationEvidence, ...]:
        if self._closed:
            raise RuntimeError("LSP session is closed")
        rows = tuple(self._session.query(
            root_id=root_id,
            symbol=_text(symbol, "symbol"),
            operation=_text(operation, "operation"),
            max_results=self._max_results,
        ))
        if len(rows) > self._max_results:
            raise ValueError("LSP adapter exceeded the result bound")
        if any(not isinstance(row, NavigationEvidence) or row.root_id != root_id for row in rows):
            raise ValueError("LSP adapter returned invalid or cross-root evidence")
        return rows

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._session.close()

    def __enter__(self) -> "LiveLspProvider":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


def open_live_lsp(
    context: MultiRootReadContext,
    transport: LspTransport,
    *,
    root_id: str,
    language: str,
    operations: Iterable[str],
    max_results: int = 100,
) -> LiveLspProvider:
    """Open a bounded LSP session only for a visible root."""
    root = context.root(root_id)
    language = _text(language, "language").casefold()
    operation_set = frozenset(_text(value, "operation") for value in operations)
    if not operation_set:
        raise ValueError("at least one LSP operation is required")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10_000:
        raise ValueError("max_results must be between 1 and 10000")
    session = transport.open(
        root_id=root.root_id,
        language=language,
        operations=operation_set,
        max_results=max_results,
    )
    if session is None:
        raise LookupError("LSP transport did not provide a session")
    return LiveLspProvider(session, max_results=max_results)


@dataclass(frozen=True, slots=True)
class MultiRepositoryNavigationResult:
    root_id: str
    backend: NavigationBackend
    evidence: tuple[NavigationEvidence, ...]


class MultiRepositoryNavigator:
    """Query explicitly selected repository ports with one global result bound."""

    def __init__(self, repositories: Iterable[RepositoryNavigationPort], *, max_results: int = 100) -> None:
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10_000:
            raise ValueError("max_results must be between 1 and 10000")
        self._repositories = tuple(repositories)
        if not self._repositories:
            raise ValueError("at least one repository port is required")
        roots = tuple(repository.root for repository in self._repositories)
        self._context = MultiRootReadContext(roots)
        self._max_results = max_results

    @property
    def context(self) -> MultiRootReadContext:
        return self._context

    def query(
        self,
        *,
        symbol: str,
        operation: str,
        lsp_by_root: dict[str, NavigationProvider] | None = None,
    ) -> tuple[MultiRepositoryNavigationResult, ...]:
        symbol = _text(symbol, "symbol")
        operation = _text(operation, "operation")
        lsp_by_root = dict(lsp_by_root or {})
        results: list[MultiRepositoryNavigationResult] = []
        remaining = self._max_results
        for repository in self._repositories:
            if remaining <= 0:
                break
            root = repository.root
            language = repository.language_for(symbol)
            provider = lsp_by_root.get(root.root_id)
            if provider is not None:
                backend = NavigationBackend("lsp", root.root_id, "live provider supplied", root.root_id)
            else:
                provider = repository.indexed_provider()
                if provider is not None:
                    backend = NavigationBackend("indexed", root.root_id, "live LSP provider not supplied")
                else:
                    provider = repository.lexical_provider()
                    backend = NavigationBackend("lexical", root.root_id, "LSP and indexed capability unavailable")
            if provider is None:
                raise LookupError(f"no navigation provider for root {root.root_id}")
            rows = tuple(provider.query(root.root_id, symbol, operation))[:remaining]
            if any(row.root_id != root.root_id for row in rows):
                raise ValueError("navigation provider returned cross-root evidence")
            results.append(MultiRepositoryNavigationResult(root.root_id, backend, rows))
            remaining -= len(rows)
        return tuple(results)


class LspNegotiator:
    """Select LSP, indexed, or lexical navigation without guessing availability."""

    def __init__(self, capabilities: Iterable[LspCapabilities] = ()) -> None:
        self._capabilities = tuple(capabilities)

    def select(self, root_id: str, language: str, operation: str, *, indexed_available: bool) -> NavigationBackend:
        language = _text(language, "language").casefold()
        operation = _text(operation, "operation")
        for capability in self._capabilities:
            if capability.root_id == root_id and language in capability.languages and operation in capability.operations:
                return NavigationBackend("lsp", root_id, "negotiated capability", capability.server_id)
        if indexed_available:
            return NavigationBackend("indexed", root_id, "LSP capability unavailable")
        return NavigationBackend("lexical", root_id, "LSP and indexed capability unavailable")


@dataclass(frozen=True, slots=True)
class OwnedWrite:
    root_id: str
    actor: str
    expected_git_revision: str


def authorize_write(context: MultiRootReadContext, request: OwnedWrite) -> None:
    """Fail closed unless the request names the context's owner and revision."""
    root = context.root(request.root_id)
    if not context.can_write(request.root_id, request.actor):
        raise PermissionError("write ownership is not granted for this root")
    if root.git_revision != request.expected_git_revision:
        raise RuntimeError("write revision does not match the independently tracked root")


def bind_navigation_evidence(
    context: MultiRootReadContext,
    root_id: str,
    path: str,
    content: bytes,
    *,
    symbol: str,
    relation: str,
    source: str,
) -> NavigationEvidence:
    """Create legacy navigation evidence only after binding root/digest/revision."""
    root = context.root(root_id)
    evidence = FileRevisionEvidence.from_bytes(root_id, path, content, root.git_revision)
    return NavigationEvidence(root_id, evidence.path, symbol, relation, source, evidence.git_revision)


def query_with_fallback(
    backend: NavigationBackend,
    *,
    lsp: NavigationProvider | None = None,
    indexed: NavigationProvider | None = None,
    lexical: NavigationProvider | None = None,
    symbol: str,
    operation: str,
) -> tuple[NavigationEvidence, ...]:
    """Invoke only the negotiated provider; missing providers fail closed."""
    provider = {"lsp": lsp, "indexed": indexed, "lexical": lexical}.get(backend.mode)
    if provider is None:
        raise LookupError(f"no provider supplied for {backend.mode} navigation")
    return tuple(provider.query(backend.root_id, symbol, operation))
