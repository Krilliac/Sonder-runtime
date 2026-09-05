"""Semantic-recall use case independent of legacy modules and storage."""
from __future__ import annotations

from ..ports.recall import (
    EmbedFunction,
    Embedding,
    RecallGateway,
    validate_recall_request,
)


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

    def retrieve_page(
        self, connection: object, task: str, *, k: int = 2,
        embed_fn: EmbedFunction | None = None, min_sim: float | None = None,
        qv: Embedding | None = None, exclude_session: str | None = None,
        project: str | None = None, include_all_projects: bool = False,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
        candidate_cursor: str | None = None,
    ):
        """Return the additive explained-recall page contract.

        The legacy list result remains the default path.  Gateways that have
        not adopted explained recall fail clearly instead of silently dropping
        provenance, which keeps memory mobility and UI explanations honest.
        """
        validate_recall_request(task, k, min_sim)
        method = getattr(self._gateway, "recall_page", None)
        if method is None:
            raise RuntimeError("explained recall is not configured")
        return method(
            connection, task, k=k, embed_fn=embed_fn, min_sim=min_sim,
            qv=qv, exclude_session=exclude_session, project=project,
            include_all_projects=include_all_projects,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
            candidate_cursor=candidate_cursor,
        )
