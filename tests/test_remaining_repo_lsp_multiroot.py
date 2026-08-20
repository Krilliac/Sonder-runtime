from __future__ import annotations

import hashlib

import pytest

from sonder_runtime.application.repository_intelligence.lsp_multiroot import (
    FileRevisionEvidence,
    LspCapabilities,
    LspNegotiator,
    MultiRootReadContext,
    NavigationBackend,
    OwnedWrite,
    RepositoryRoot,
    authorize_write,
    bind_navigation_evidence,
    query_with_fallback,
)


def test_capability_negotiation_prefers_lsp_then_index_then_lexical():
    negotiator = LspNegotiator((LspCapabilities("pyright", "app", frozenset({"python"}), frozenset({"definition"}), "r1"),))
    assert negotiator.select("app", "Python", "definition", indexed_available=True).mode == "lsp"
    assert negotiator.select("app", "rust", "definition", indexed_available=True).mode == "indexed"
    assert negotiator.select("app", "rust", "definition", indexed_available=False).mode == "lexical"


def test_multi_root_reads_keep_independent_revisions_and_write_owner():
    context = MultiRootReadContext((RepositoryRoot("app", "app", "a1", "agent"), RepositoryRoot("lib", "lib", "l1", "other")), "app")
    assert context.root("lib").git_revision == "l1"
    assert context.can_write("app", "agent")
    assert not context.can_write("lib", "agent")
    authorize_write(context, OwnedWrite("app", "agent", "a1"))
    with pytest.raises(PermissionError):
        authorize_write(context, OwnedWrite("lib", "agent", "l1"))
    with pytest.raises(RuntimeError):
        authorize_write(context, OwnedWrite("app", "agent", "stale"))


def test_navigation_evidence_binds_digest_and_root_revision():
    content = b"def target(): pass\n"
    context = MultiRootReadContext((RepositoryRoot("app", "app", "commit-7"),))
    evidence = bind_navigation_evidence(context, "app", "src\\mod.py", content, symbol="target", relation="caller", source="lsp")
    assert evidence.file_path == "src/mod.py"
    assert evidence.revision == "commit-7"
    assert FileRevisionEvidence.from_bytes("app", "src/mod.py", content, "commit-7").sha256 == hashlib.sha256(content).hexdigest()


class _Provider:
    def __init__(self, marker: str):
        self.marker = marker

    def query(self, root_id: str, symbol: str, operation: str):
        from sonder_runtime.application.repository_intelligence.navigation import NavigationEvidence
        return (NavigationEvidence(root_id, f"{self.marker}.py", symbol, operation, self.marker, "rev"),)


def test_query_with_fallback_uses_only_negotiated_backend():
    backend = NavigationBackend("lexical", "app", "fallback")
    result = query_with_fallback(backend, lsp=_Provider("lsp"), indexed=_Provider("index"), lexical=_Provider("lex"), symbol="target", operation="definition")
    assert result[0].source == "lex"
    with pytest.raises(LookupError):
        query_with_fallback(NavigationBackend("lsp", "app", "required"), symbol="target", operation="definition")

