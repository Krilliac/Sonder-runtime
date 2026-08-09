"""Port for semantic recall over the caller-owned memory connection."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import math
from typing import Any, Protocol

from ...domain.common.errors import InvalidInput


Embedding = Sequence[float]
EmbedFunction = Callable[[str], Embedding | None]
MAX_RECALL_RESULTS = 20
MAX_RECALL_QUERY_CHARS = 64_000


def validate_recall_request(task: str, k: int, min_sim: float | None) -> None:
    """Reject requests that could make recall consume unbounded work."""
    if not isinstance(task, str):
        raise InvalidInput("recall task must be text")
    if len(task) > MAX_RECALL_QUERY_CHARS:
        raise InvalidInput(
            f"recall task exceeds {MAX_RECALL_QUERY_CHARS} characters"
        )
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_RECALL_RESULTS:
        raise InvalidInput(
            f"recall limit must be an integer from 1 to {MAX_RECALL_RESULTS}"
        )
    if min_sim is not None:
        if (
            isinstance(min_sim, bool)
            or not isinstance(min_sim, (int, float))
            or not math.isfinite(min_sim)
            or not -1.0 <= min_sim <= 1.0
        ):
            raise InvalidInput("recall similarity threshold must be between -1 and 1")


class RecallGateway(Protocol):
    def recall(
        self,
        connection: Any,
        task: str,
        *,
        k: int = 2,
        embed_fn: EmbedFunction | None = None,
        min_sim: float | None = None,
        qv: Embedding | None = None,
        exclude_session: str | None = None,
        project: str | None = None,
        include_all_projects: bool = False,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
    ) -> list[str]: ...
