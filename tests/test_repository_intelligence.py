"""WP4 REPO-001/003/005 application contract tests."""
from __future__ import annotations

import hashlib

import pytest

from sonder_runtime.application.repository_intelligence import FileEvidence, IndexDelta, RepositoryIndex, SymbolRecord
from sonder_runtime.application.repository_intelligence.index_map import digest_bytes


def evidence(path="src/main.py", revision="rev-1"):
    return FileEvidence(path, hashlib.sha256(path.encode()).hexdigest(), revision)


def symbol(symbol_id, name, *, path="src/main.py", line=1, calls=(), cost=1):
    return SymbolRecord(symbol_id, name, "function", "python", evidence(path), line=line, calls=calls, token_cost=cost)


def test_incremental_delta_replaces_and_removes_records_without_scanning():
    index = RepositoryIndex([symbol("a", "alpha")])
    assert index.snapshot()["a"].name == "alpha"
    assert index.apply(IndexDelta((symbol("a", "updated", line=4), symbol("b", "beta")))) == 2
    assert index.snapshot()["a"].line == 4
    index.apply(IndexDelta(removed_symbol_ids=("b",)))
    assert tuple(index.snapshot()) == ("a",)


def test_replace_file_requires_exact_shared_evidence():
    index = RepositoryIndex([symbol("a", "alpha")])
    file_evidence = evidence()
    index.replace_file(file_evidence, [symbol("new", "new_name")])
    assert tuple(index.snapshot()) == ("new",)
    with pytest.raises(ValueError, match="supplied file evidence"):
        index.replace_file(file_evidence, [symbol("bad", "bad", path="other.py")])


def test_ranked_map_is_deterministic_and_respects_token_budget():
    index = RepositoryIndex([
        symbol("caller", "handle_request", line=2, calls=("target",), cost=4),
        symbol("target", "target", line=8, cost=3),
        symbol("other", "unrelated", path="z.py", cost=3),
    ])
    result = index.ranked_map("target", token_budget=7)
    assert [entry.record.symbol_id for entry in result.entries] == ["target", "caller"]
    assert result.total_tokens == 7
    assert result.entries[1].relation_hits == ("target",)


def test_digest_helper_and_evidence_are_exact():
    content = b"exact bytes"
    assert digest_bytes(content) == hashlib.sha256(content).hexdigest()
    assert evidence("src\\main.py").path == "src/main.py"
    with pytest.raises(ValueError, match="SHA-256"):
        FileEvidence("x.py", "not-a-digest")
