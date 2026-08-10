"""Thin CLI commands (SPEC-5 §28).

Each command: parse argv → create OperationContext → call service → format output → exit code.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import sys
import uuid
from typing import Any, TextIO

from ...application.context import local_owner_context
from ...application.errors import SonderError


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
