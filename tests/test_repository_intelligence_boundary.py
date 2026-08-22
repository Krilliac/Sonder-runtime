"""Focused evidence for the REPO-001..007 application boundary."""
from __future__ import annotations

import hashlib
import io
import json

from sonder_runtime.application.repository_intelligence import (
    FileEvidence,
    IndexDelta,
    RepositoryIntelligenceFacade,
    SymbolRecord,
)
from sonder_runtime.application.repository_intelligence.navigation import (
    ExpansionRequest,
    NavigationEvidence,
)
from sonder_runtime.interfaces.cli.commands import RepositoryMapCommand


def _record(symbol_id: str, name: str, *, line: int = 1, cost: int = 1) -> SymbolRecord:
    return SymbolRecord(
        symbol_id, name, "function", "python",
        FileEvidence("src/main.py", hashlib.sha256(b"main").hexdigest(), "rev-1"),
        line=line, token_cost=cost,
    )


def test_facade_composes_incremental_map_and_bounded_expansion() -> None:
    facade = RepositoryIntelligenceFacade([_record("one", "target", cost=2)])
    assert facade.generation == 1
    facade.apply(IndexDelta((_record("two", "caller", line=2, cost=2),)))
    result = facade.ranked_map("target", token_budget=2)
    assert [entry.record.symbol_id for entry in result.entries] == ["one"]

    evidence = (
        NavigationEvidence("app", "a.py", "target", "caller", revision="rev-1"),
        NavigationEvidence("app", "b.py", "caller", "test", revision="rev-1"),
    )
    expanded = facade.expand(evidence, ExpansionRequest(("app",), ("target",), max_symbols=1))
    assert expanded == evidence[:1]


def test_repository_map_command_is_bounded_json_and_carries_evidence() -> None:
    out = io.StringIO()
    code = RepositoryMapCommand(RepositoryIntelligenceFacade([_record("one", "target")])).run(
        "target", token_budget=1, out=out
    )
    payload = json.loads(out.getvalue())
    assert code == 0
    assert payload["object"] == "repository_map"
    assert payload["total_tokens"] == 1
    assert payload["entries"][0]["git_revision"] == "rev-1"
    assert payload["entries"][0]["sha256"] == hashlib.sha256(b"main").hexdigest()
