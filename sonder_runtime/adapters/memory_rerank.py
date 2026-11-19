"""Embedding-backed adapters for pure memory ranking policy."""
from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

import sonder_runtime.adapters.embeddings as embeddings
from sonder_runtime.domain.memory import rules

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

_ItemT = TypeVar("_ItemT")
_VecT = TypeVar("_VecT")
_BlobT = TypeVar("_BlobT")


def mmr_rerank(
    query_vec: _VecT | None,
    candidates: Sequence[tuple[_ItemT, _VecT]],
    k: int = 5,
    lambda_mult: float = 0.5,
    sim_fn: Callable[[_VecT, _VecT], float] = embeddings.cosine,
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
    sim_fn: Callable[[_VecT, _VecT], float] = embeddings.cosine,
    from_blob: Callable[[_BlobT], _VecT] = embeddings.from_blob,
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
