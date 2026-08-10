"""Thin REPL handler (SPEC-5 §28).

Parses line input → create OperationContext → call service → print result.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import uuid
from typing import Any, TextIO

from ...application.context import local_owner_context
from ...application.errors import SonderError


class ReplHandler:
    """Dispatches single-line REPL commands to application services."""

    def __init__(
        self,
        recall_service=None,
        outcome_service=None,
    ):
        self._recall = recall_service
        self._outcome = outcome_service

    def dispatch(self, line: str, out: TextIO | None = None) -> str:
        import sys
        out = out or sys.stdout
        parts = line.strip().split(None, 2)
        if not parts:
            return ""

        cmd = parts[0].lower()
        ctx = local_owner_context(
            correlation_id=uuid.uuid4().hex,
            source="repl",
        )

        try:
            if cmd == "recall" and self._recall is not None:
                task = parts[1] if len(parts) > 1 else ""
                results = self._recall.recall(task, k=2)
                output = "\n".join(results) if results else "(no results)"
                out.write(output + "\n")
                return output

            if cmd == "outcome" and self._outcome is not None:
                if len(parts) < 3:
                    msg = "usage: outcome <interaction_id> <signal>"
                    out.write(msg + "\n")
                    return msg
                score = self._outcome.record(parts[1], parts[2])
                output = f"score: {score}"
                out.write(output + "\n")
                return output

            msg = f"unknown command: {cmd}"
            out.write(msg + "\n")
            return msg

        except SonderError as e:
            msg = f"error: {e.code} {e}"
            out.write(msg + "\n")
            return msg
