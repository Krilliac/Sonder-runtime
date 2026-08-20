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
from typing import Callable, Iterable, Protocol, Sequence

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

