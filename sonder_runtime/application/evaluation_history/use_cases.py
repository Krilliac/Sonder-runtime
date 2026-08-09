"""Evaluation-history reads independent of MCP and persistence details."""
from __future__ import annotations

from typing import Mapping

from ..ports.evaluation_history import EvaluationHistoryReader


class EvaluationHistoryService:
    """Expose identity-safe trend reads through the application boundary."""

    def __init__(self, reader: EvaluationHistoryReader) -> None:
        self._reader = reader

    def status(
        self,
        *,
        model: str = "",
        model_digest: str = "",
        suite: str = "",
        suite_version: str = "",
        suite_digest: str = "",
        tolerance: float = 0.0,
        max_records: int = 10_000,
    ) -> Mapping[str, object]:
        result = self._reader.status(
            model=model,
            model_digest=model_digest,
            suite=suite,
            suite_version=suite_version,
            suite_digest=suite_digest,
            tolerance=tolerance,
            max_records=max_records,
        )
        if not isinstance(result, Mapping):
            raise TypeError("evaluation history reader returned a non-mapping payload")
        if not isinstance(result.get("groups"), list):
            raise TypeError("evaluation history reader groups must be a list")
        return result
