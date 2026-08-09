"""Semantic-recall use case independent of legacy modules and storage."""
from __future__ import annotations

import math

from ...domain.common.errors import InvalidInput
from ..ports.recall import EmbedFunction, Embedding, RecallGateway


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


class RecallService:
    def __init__(self, gateway: RecallGateway) -> None:
        self._gateway = gateway

    def retrieve(
        self, connection: object, task: str, *, k: int = 2,
        embed_fn: EmbedFunction | None = None, min_sim: float | None = None,
        qv: Embedding | None = None, exclude_session: str | None = None,
        project: str | None = None, include_all_projects: bool = False,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
    ) -> list[str]:
        validate_recall_request(task, k, min_sim)
        return self._gateway.recall(
            connection, task, k=k, embed_fn=embed_fn, min_sim=min_sim,
            qv=qv, exclude_session=exclude_session, project=project,
            include_all_projects=include_all_projects,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )
