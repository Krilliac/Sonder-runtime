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
    MultiRepositoryNavigator,
    open_live_lsp,
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


class _Session:
    def __init__(self):
        self.calls = []
        self.closed = False

    def query(self, **kwargs):
        self.calls.append(kwargs)
        from sonder_runtime.application.repository_intelligence.navigation import NavigationEvidence
        return (NavigationEvidence(kwargs["root_id"], "src/target.py", kwargs["symbol"], kwargs["operation"], "lsp", "r1"),)

    def close(self):
        self.closed = True


class _Transport:
    def __init__(self):
        self.session = _Session()
        self.opened = None

    def open(self, **kwargs):
        self.opened = kwargs
        return self.session


def test_live_lsp_seam_opens_bounds_queries_and_closes_session():
    transport = _Transport()
    context = MultiRootReadContext((RepositoryRoot("app", "app", "r1"),))
    provider = open_live_lsp(context, transport, root_id="app", language="Python", operations=("definition",), max_results=3)
    rows = provider.query("app", "target", "definition")
    assert rows[0].source == "lsp"
    assert transport.opened["language"] == "python"
    assert transport.session.calls[0]["max_results"] == 3
    provider.close()
    assert transport.session.closed
    with pytest.raises(RuntimeError):
        provider.query("app", "target", "definition")


class _Repo:
    def __init__(self, root_id, revision, source):
        self.root = RepositoryRoot(root_id, root_id, revision)
        self.source = source

    def language_for(self, _symbol):
        return "python"

    def indexed_provider(self):
        return _Provider(self.source)

    def lexical_provider(self):
        return _Provider("lexical")


def test_multi_repository_navigator_reads_each_root_with_global_bound():
    navigator = MultiRepositoryNavigator((_Repo("app", "a1", "app-index"), _Repo("lib", "l1", "lib-index")), max_results=1)
    results = navigator.query(symbol="target", operation="definition")
    assert len(results) == 1
    assert results[0].root_id == "app"
    assert results[0].evidence[0].root_id == "app"
    assert navigator.context.root("lib").git_revision == "l1"


def test_multi_repository_navigator_uses_explicit_live_provider_per_root():
    navigator = MultiRepositoryNavigator((_Repo("app", "a1", "app-index"),))
    live = _Provider("live")
    result = navigator.query(symbol="target", operation="definition", lsp_by_root={"app": live})
    assert result[0].backend.mode == "lsp"
    assert result[0].backend.server_id == "app"
    assert result[0].evidence[0].source == "live"


def test_multi_repository_navigator_rejects_cross_root_provider_result():
    class BadRepo(_Repo):
        def indexed_provider(self):
            class Bad(_Provider):
                def query(self, _root_id, symbol, operation):
                    from sonder_runtime.application.repository_intelligence.navigation import NavigationEvidence
                    return (NavigationEvidence("other", "bad.py", symbol, operation),)
            return Bad("bad")

    with pytest.raises(ValueError, match="cross-root"):
        MultiRepositoryNavigator((BadRepo("app", "a1", "bad"),)).query(symbol="x", operation="definition")
