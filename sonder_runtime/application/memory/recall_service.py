"""Recall orchestration service (SPEC-5 §15).

Owns the recall pipeline: lexical search → semantic search →
similarity filtering → MMR diversification. Delegates to ports
for persistence and embeddings — never touches SQLite or Ollama
directly.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import NotRequired, TypeAlias

    SimilarityFn: TypeAlias = Callable[[Sequence[float], Sequence[float]], float]

from ...domain.memory.rules import (
    DEFAULT_RECALL_MIN_SIM,
    mmr_select,
    passes_similarity,
)
from ..ports.recall import validate_recall_request

logger = logging.getLogger(__name__)


class LexicalHit(TypedDict):
    text: str


class SemanticCandidate(TypedDict):
    id: str
    text: str
    embedding: NotRequired[Sequence[float] | None]
    embedding_model: NotRequired[str | None]
    embedding_revision: NotRequired[str | None]


class EmbeddingPort(Protocol):
    def embed(self, text: str) -> list[float] | None: ...


class RecallStore(Protocol):
    """Read-side port for the memory store's recall data."""

    def lexical_search(
        self, query: str, *, k: int, project: str | None,
        exclude_session: str | None,
    ) -> Sequence[LexicalHit]: ...

    def semantic_candidates(
        self, *, k: int, project: str | None,
        exclude_session: str | None,
        embedding_model: str | None,
        embedding_revision: str | None,
    ) -> Sequence[SemanticCandidate]: ...


class RecallService:
    """Application-level recall orchestration.

    Composes lexical FTS, semantic similarity, and MMR into a single
    recall pipeline. All I/O goes through injected ports.
    """

    def __init__(
        self,
        store: RecallStore,
        embed: EmbeddingPort | None = None,
        sim_fn: SimilarityFn | None = None,
    ) -> None:
        self._store = store
        self._embed = embed
        self._sim_fn: SimilarityFn = sim_fn or _cosine_similarity

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
        logger.debug(f"RecallService.recall: k={k}, min_sim={min_sim}, project={project!r}, has_embedder={self._embed is not None}")
        validate_recall_request(task, k, min_sim)
        effective_min_sim = min_sim if min_sim is not None else DEFAULT_RECALL_MIN_SIM

        lexical = self._store.lexical_search(
            task, k=k, project=project, exclude_session=exclude_session,
        )

        if self._embed is None:
            logger.warning("no embedding port configured, falling back to lexical-only recall")
            logger.debug(f"RecallService.recall: no embedder, returning {min(len(lexical), k)} lexical results")
            return [r["text"] for r in lexical[:k]]

        query_vec = self._embed.embed(task)
        if query_vec is not None and not _valid_vector(query_vec):
            logger.warning("query embedding is empty or non-finite, falling back to lexical-only recall")
            return [r["text"] for r in lexical[:k]]
        if query_vec is None:
            logger.error(f"embedding failed for recall query, falling back to lexical-only: project={project!r}")
            logger.warning("embedding returned None (embedder may be overloaded), falling back to lexical-only recall")
            logger.debug(f"RecallService.recall: embedding returned None, falling back to {min(len(lexical), k)} lexical results")
            return [r["text"] for r in lexical[:k]]

        candidates = self._store.semantic_candidates(
            k=k * 3,
            project=project,
            exclude_session=exclude_session,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )

        scored: list[tuple[str, Sequence[float], float, str]] = []
        for c in candidates:
            vec = c.get("embedding")
            if vec is None or not _valid_vector(vec) or len(vec) != len(query_vec):
                continue
            if embedding_model and c.get("embedding_model") != embedding_model:
                continue
            if (
                embedding_revision is not None
                and (c.get("embedding_revision") or None) != (embedding_revision or None)
            ):
                continue
            sim = self._sim_fn(query_vec, vec)
            if passes_similarity(sim, effective_min_sim):
                scored.append((c["id"], vec, sim, c["text"]))

        if not scored:
            logger.warning(f"no semantic candidates passed similarity threshold (min_sim={effective_min_sim}), falling back to lexical results")
            logger.debug(f"RecallService.recall: no candidates passed similarity threshold, returning lexical results")
            return [r["text"] for r in lexical[:k]]

        logger.debug(f"RecallService.recall: {len(scored)} candidates passed similarity, running MMR selection for k={k}")
        mmr_ids = mmr_select(
            query_vec,
            [(cid, vec) for cid, vec, _, _ in scored],
            k=k,
            sim_fn=self._sim_fn,
        )

        id_to_text = {cid: text for cid, _, _, text in scored}
        return [id_to_text[cid] for cid in mmr_ids if cid in id_to_text]


def _valid_vector(v: Sequence[float]) -> bool:
    """Non-empty, all finite numbers, not the zero vector."""
    return len(v) > 0 and any(x != 0 for x in v) and all(
        isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
        for x in v
    )


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 for unequal length, empty, non-finite or zero-norm input."""
    if len(a) != len(b) or not _valid_vector(a) or not _valid_vector(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a: float = sum(x * x for x in a) ** 0.5
    norm_b: float = sum(x * x for x in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
