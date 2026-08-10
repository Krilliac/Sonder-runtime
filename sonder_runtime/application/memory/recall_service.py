"""Recall orchestration service (SPEC-5 §15).

Owns the recall pipeline: lexical search → semantic search →
similarity filtering → MMR diversification. Delegates to ports
for persistence and embeddings — never touches SQLite or Ollama
directly.
"""
from __future__ import annotations

from typing import Protocol, Sequence

from ...domain.memory.rules import (
    DEFAULT_RECALL_MIN_SIM,
    mmr_select,
    passes_similarity,
)
from ..ports.recall import validate_recall_request


class EmbeddingPort(Protocol):
    def embed(self, text: str) -> list[float] | None: ...


class RecallStore(Protocol):
    """Read-side port for the memory store's recall data."""

    def lexical_search(
        self, query: str, *, k: int, project: str | None,
        exclude_session: str | None,
    ) -> list[dict]: ...

    def semantic_candidates(
        self, *, k: int, project: str | None,
        exclude_session: str | None,
        embedding_model: str | None,
        embedding_revision: str | None,
    ) -> list[dict]: ...


class RecallService:
    """Application-level recall orchestration.

    Composes lexical FTS, semantic similarity, and MMR into a single
    recall pipeline. All I/O goes through injected ports.
    """

    def __init__(
        self,
        store: RecallStore,
        embed: EmbeddingPort | None = None,
        sim_fn=None,
    ):
        self._store = store
        self._embed = embed
        self._sim_fn = sim_fn or _cosine_similarity

    def recall(
        self,
        task: str,
        *,
        k: int = 2,
        min_sim: float | None = None,
        project: str | None = None,
        exclude_session: str | None = None,
        embedding_model: str | None = None,
        embedding_revision: str | None = None,
    ) -> list[str]:
        validate_recall_request(task, k, min_sim)
        effective_min_sim = min_sim if min_sim is not None else DEFAULT_RECALL_MIN_SIM

        lexical = self._store.lexical_search(
            task, k=k, project=project, exclude_session=exclude_session,
        )

        if self._embed is None:
            return [r["text"] for r in lexical[:k]]

        query_vec = self._embed.embed(task)
        if query_vec is None:
            return [r["text"] for r in lexical[:k]]

        candidates = self._store.semantic_candidates(
            k=k * 3,
            project=project,
            exclude_session=exclude_session,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )

        scored = []
        for c in candidates:
            vec = c.get("embedding")
            if vec is None:
                continue
            sim = self._sim_fn(query_vec, vec)
            if passes_similarity(sim, effective_min_sim):
                scored.append((c["id"], vec, sim, c["text"]))

        if not scored:
            return [r["text"] for r in lexical[:k]]

        mmr_ids = mmr_select(
            query_vec,
            [(cid, vec) for cid, vec, _, _ in scored],
            k=k,
            sim_fn=self._sim_fn,
        )

        id_to_text = {cid: text for cid, _, _, text in scored}
        return [id_to_text[cid] for cid in mmr_ids if cid in id_to_text]


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
