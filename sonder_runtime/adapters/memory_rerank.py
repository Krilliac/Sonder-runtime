"""Embedding-backed MMR and optional cross-encoder neural rerank adapters."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, TypeVar

import sonder_runtime.adapters.embeddings
from sonder_runtime.application.chat.provider_bridge import UnsupportedProviderFeature
from sonder_runtime.application.context import (
    OperationContext,
    current_operation_context,
)
from sonder_runtime.application.ports.model_gateway import RerankScore
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    InvalidInput,
)
from sonder_runtime.domain.memory import rules

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

_ItemT = TypeVar("_ItemT")
_VecT = TypeVar("_VecT")
_BlobT = TypeVar("_BlobT")

logger = logging.getLogger(__name__)


class CrossEncoderReranker(Protocol):
    """Port for an optional cross-encoder neural reranking adapter."""

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        context: OperationContext | None = None,
        *,
        model: str | None = None,
        top_n: int | None = None,
    ) -> Sequence[RerankScore]: ...


def mmr_rerank(
    query_vec: _VecT | None,
    candidates: Sequence[tuple[_ItemT, _VecT]],
    k: int = 5,
    lambda_mult: float = 0.5,
    sim_fn: Callable[[_VecT, _VecT], float] = sonder_runtime.adapters.embeddings.cosine,
) -> list[_ItemT]:
    """Select relevant, diverse candidates with the configured similarity."""
    return rules.mmr_select(
        query_vec,
        candidates,
        k=k,
        lambda_mult=lambda_mult,
        sim_fn=sim_fn,
    )


def mmr_from_blobs(
    query_vec: _VecT | None,
    id_blob_pairs: Iterable[tuple[_ItemT, _BlobT | None]],
    k: int = 5,
    lambda_mult: float = 0.5,
    sim_fn: Callable[[_VecT, _VecT], float] = sonder_runtime.adapters.embeddings.cosine,
    from_blob: Callable[[_BlobT], _VecT] = sonder_runtime.adapters.embeddings.from_blob,
) -> list[_ItemT]:
    """Decode stored embeddings, omit empty values, and apply MMR."""
    candidates = [
        (candidate_id, from_blob(blob))
        for candidate_id, blob in id_blob_pairs
        if blob
    ]
    return mmr_rerank(
        query_vec,
        candidates,
        k=k,
        lambda_mult=lambda_mult,
        sim_fn=sim_fn,
    )


def neural_rerank(
    query: str,
    candidates: Sequence[tuple[_ItemT, str]],
    *,
    reranker: CrossEncoderReranker,
    k: int = 5,
    model: str | None = None,
    context: OperationContext | None = None,
) -> list[_ItemT]:
    """Rank *(item, text)* candidates using *reranker*, preserving original indices."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidInput("neural_rerank query must be a non-empty string")
    if k <= 0 or not candidates:
        return []
    docs: list[str] = []
    for _item, text in candidates:
        if not isinstance(text, str):
            raise InvalidInput("neural_rerank candidate texts must be strings")
        docs.append(text)
    scores = list(
        reranker.rerank(
            query,
            docs,
            context=context,
            model=model,
            top_n=min(k, len(docs)),
        )
    )
    seen: set[int] = set()
    validated: list[RerankScore] = []
    for entry in scores:
        idx = entry.index
        if type(idx) is not int or not 0 <= idx < len(candidates) or idx in seen:
            raise DependencyUnavailable("reranker returned out-of-range or duplicate candidate index")
        seen.add(idx)
        validated.append(entry)
    validated.sort(key=lambda item: (-item.relevance_score, item.index))
    return [candidates[entry.index][0] for entry in validated[:k]]


def _resolve_candidate_texts(
    candidates: Sequence[tuple[_ItemT, _VecT]],
    candidate_texts: Sequence[str] | Mapping[_ItemT, str] | None,
) -> list[tuple[_ItemT, str]] | None:
    if candidate_texts is None or isinstance(candidate_texts, (str, bytes)):
        return None
    if isinstance(candidate_texts, (list, tuple)):
        if len(candidate_texts) != len(candidates):
            return None
        resolved: list[tuple[_ItemT, str]] = []
        for (item, _vec), text in zip(candidates, candidate_texts, strict=True):
            if not isinstance(text, str):
                return None
            resolved.append((item, text))
        return resolved
    if isinstance(candidate_texts, dict) or hasattr(candidate_texts, "get"):
        mapped: list[tuple[_ItemT, str]] = []
        for item, _vec in candidates:
            text = candidate_texts.get(item)
            if not isinstance(text, str):
                return None
            mapped.append((item, text))
        return mapped
    seq = list(candidate_texts)
    if len(seq) != len(candidates) or any(not isinstance(text, str) for text in seq):
        return None
    return [(item, text) for (item, _vec), text in zip(candidates, seq, strict=True)]


def rerank_candidates(
    query_vec: _VecT | None,
    candidates: Sequence[tuple[_ItemT, _VecT]],
    *,
    k: int = 5,
    lambda_mult: float = 0.5,
    sim_fn: Callable[[_VecT, _VecT], float] = sonder_runtime.adapters.embeddings.cosine,
    query_text: str | None = None,
    candidate_texts: Sequence[str] | Mapping[_ItemT, str] | None = None,
    reranker: CrossEncoderReranker | None = None,
    rerank_model: str | None = None,
    context: OperationContext | None = None,
) -> list[_ItemT]:
    """Rerank candidates with an optional neural cross-encoder, keeping MMR default."""
    if k <= 0 or not candidates:
        return []
    active_context = context if context is not None else current_operation_context()
    text_pairs = _resolve_candidate_texts(candidates, candidate_texts)
    if (
        reranker is not None
        and isinstance(query_text, str)
        and query_text.strip()
        and text_pairs is not None
        and active_context is not None
    ):
        try:
            return neural_rerank(
                query_text,
                text_pairs,
                reranker=reranker,
                k=k,
                model=rerank_model,
                context=active_context,
            )
        except (
            DependencyUnavailable,
            UnsupportedProviderFeature,
            CapacityExceeded,
            DeadlineExceeded,
        ) as exc:
            logger.warning(
                "neural rerank unavailable (%s); falling back to MMR",
                type(exc).__name__,
            )
    return mmr_rerank(
        query_vec,
        candidates,
        k=k,
        lambda_mult=lambda_mult,
        sim_fn=sim_fn,
    )


__all__ = [
    "CrossEncoderReranker",
    "mmr_from_blobs",
    "mmr_rerank",
    "neural_rerank",
    "rerank_candidates",
]
