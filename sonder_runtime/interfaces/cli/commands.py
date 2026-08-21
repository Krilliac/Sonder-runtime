"""Thin CLI commands (SPEC-5 §28).

Each command: parse argv → create OperationContext → call service → format output → exit code.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import sys
import uuid
import json
from typing import Any, TextIO

from ...application.context import local_owner_context
from ...application.errors import SonderError
from ...application.ports.repository_intelligence import RepositoryIntelligencePort


def _make_context(source: str = "repl"):
    return local_owner_context(
        correlation_id=uuid.uuid4().hex,
        source=source,
    )


class StatusCommand:
    """sonder status — delegates to application health/status service."""

    def __init__(self, health_service=None):
        self._health = health_service

    def run(self, out: TextIO = sys.stdout) -> int:
        if self._health is None:
            out.write("status: ok (no health service)\n")
            return 0
        try:
            report = self._health.check()
            out.write(f"status: {report}\n")
        except SonderError as e:
            out.write(f"error: {e.code} {e}\n")
            return 1
        return 0


class RecallCommand:
    """sonder recall <task> — delegates to RecallService."""

    def __init__(self, recall_service):
        self._recall = recall_service

    def run(self, task: str, *, k: int = 2, out: TextIO = sys.stdout) -> int:
        try:
            results = self._recall.recall(task, k=k)
        except SonderError as e:
            out.write(f"error: {e.code} {e}\n")
            return 1
        for r in results:
            out.write(f"{r}\n")
        return 0


class OutcomeCommand:
    """sonder outcome <id> <signal> — delegates to OutcomeService."""

    def __init__(self, outcome_service):
        self._outcome = outcome_service

    def run(self, interaction_id: str, signal: str, out: TextIO = sys.stdout) -> int:
        try:
            score = self._outcome.record(interaction_id, signal)
        except SonderError as e:
            out.write(f"error: {e.code} {e}\n")
            return 1
        out.write(f"score: {score}\n")
        return 0


class RepositoryMapCommand:
    """Inspect the bounded ranked repository map through an injected facade."""

    def __init__(self, repository_intelligence: RepositoryIntelligencePort) -> None:
        self._repository_intelligence = repository_intelligence

    def run(self, query: str = "", *, token_budget: int = 2000, out: TextIO = sys.stdout) -> int:
        try:
            result = self._repository_intelligence.ranked_map(query, token_budget=token_budget)
        except (ValueError, TypeError, SonderError) as error:
            out.write(f"error: {error}\n")
            return 1
        payload = {
            "object": "repository_map",
            "generation": self._repository_intelligence.generation,
            "query": result.query,
            "token_budget": result.token_budget,
            "total_tokens": result.total_tokens,
            "entries": [
                {
                    "symbol_id": entry.record.symbol_id,
                    "name": entry.record.name,
                    "kind": entry.record.kind,
                    "language": entry.record.language,
                    "path": entry.record.file_path,
                    "line": entry.record.line,
                    "score": entry.score,
                    "relation_hits": list(entry.relation_hits),
                    "sha256": entry.record.evidence.sha256,
                    "git_revision": entry.record.evidence.git_revision,
                }
                for entry in result.entries
            ],
        }
        out.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0
