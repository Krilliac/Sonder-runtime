"""Port for identity-separated evaluation-history reads."""
from __future__ import annotations

from typing import Mapping, Protocol


class EvaluationHistoryReader(Protocol):
    """Read aggregate evidence without running or promoting a model."""

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
    ) -> Mapping[str, object]: ...
