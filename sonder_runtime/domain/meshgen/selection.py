"""Ranking of finished candidates.  Pure.

A result is a dict with ``built`` (bool), ``within_budget`` (bool),
``score`` (int or None) and ``tris`` (int).
"""
from __future__ import annotations


def rank_key(result: dict) -> tuple:
    """Higher is better: built, then within budget, then vision score, then fewer tris."""
    built = bool(result.get("built"))
    score = result.get("score")
    tris = result.get("tris")
    return (
        1 if built else 0,
        1 if built and result.get("within_budget") else 0,
        score if isinstance(score, (int, float)) and not isinstance(score, bool) else -1,
        -(tris if isinstance(tris, (int, float)) else 0),
    )


def pick_best(results):
    """Best result by :func:`rank_key`; the earliest wins ties; ``None`` when empty."""
    best = None
    best_key = None
    for result in results:
        key = rank_key(result)
        if best is None or key > best_key:
            best, best_key = result, key
    return best
