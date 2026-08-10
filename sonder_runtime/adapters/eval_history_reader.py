"""Compatibility adapter for the historical evaluation-history contract."""
from __future__ import annotations

from typing import Mapping


class LegacyEvaluationHistoryReader:
    """Preserve the legacy dictionary shape behind the typed reader port."""

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
        import sonder_runtime.adapters.evaluation_history_store as evaluation_history_store

        return evaluation_history_store.history_status(
            model=model,
            model_digest=model_digest,
            suite=suite,
            suite_version=suite_version,
            suite_digest=suite_digest,
            tolerance=tolerance,
            max_records=max_records,
        )
