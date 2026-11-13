"""Passage selection: identifier-title priority, per-document and per-source caps, size fitting.

Ported unchanged from the lookup client.  Everything here works on rows the
adapter already read; the one store lookup it needs (title-restricted top-up)
is passed in as a callable.
"""
from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Sequence

from .models import Passage, RankedRow

PASSAGE_CHAR_LIMIT = 9000
MAX_PER_DOC = 2
PRIORITY_PER_IDENTIFIER = 2
CANDIDATE_FACTOR = 20
CANDIDATE_FLOOR = 120
CONTEXT_RESERVE_CHARS_PER_TOKEN = 3
CONTEXT_SAFETY_TOKENS = 300


def candidate_limit(k: int) -> int:
    """How many ranked rows to read before the caps are applied."""
    return max(k * CANDIDATE_FACTOR, CANDIDATE_FLOOR)


def title_has(title: str | None, ident: str) -> bool:
    """True when ident is a whole token of title: not preceded or followed by a letter or digit."""
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(ident) + r'(?![A-Za-z0-9])',
                     title or '', re.IGNORECASE) is not None


def priority_rows(
    rows: Sequence[RankedRow],
    idents: Sequence[str],
    fetch_title_rows: Callable[[str], Sequence[RankedRow]],
) -> list[RankedRow]:
    """Up to 2 passages per identifier whose title holds it as a whole token, in identifier order.

    The ranked candidate rows are used first; ``fetch_title_rows(ident)`` (a
    title-restricted query, best first) tops them up.
    """
    chosen: list[RankedRow] = []
    keys: set[tuple[int, str]] = set()
    for ident in idents:
        picked = 0
        for row in rows:
            if picked >= PRIORITY_PER_IDENTIFIER:
                break
            key = (row.doc, row.body)
            if key not in keys and title_has(row.title, ident):
                keys.add(key)
                chosen.append(row)
                picked += 1
        if picked >= PRIORITY_PER_IDENTIFIER:
            continue
        for row in fetch_title_rows(ident):
            if picked >= PRIORITY_PER_IDENTIFIER:
                break
            key = (row.doc, row.body)
            if key not in keys and title_has(row.title, ident):
                keys.add(key)
                chosen.append(row)
                picked += 1
    return chosen


def _norm_body(text: str) -> str:
    return ' '.join(text.split()).lower()


def select_passages(rows: Sequence[RankedRow], k: int) -> tuple[Passage, ...]:
    """Return up to k best passages, best first.

    Caps: 2 passages per document, max(2, k - 2) per source, and passages whose
    normalised body equals an already selected one are skipped.  ``rows`` are
    taken in order, so priority rows placed first stay first.
    """
    if k <= 0:
        return ()
    results: list[Passage] = []
    per_doc: dict[int, int] = {}
    per_source: dict[str, int] = {}
    seen: set[str] = set()
    source_cap = max(2, k - 2)
    for row in rows:
        norm = _norm_body(row.body)
        if (per_doc.get(row.doc, 0) >= MAX_PER_DOC
                or per_source.get(row.source, 0) >= source_cap or norm in seen):
            continue
        seen.add(norm)
        per_doc[row.doc] = per_doc.get(row.doc, 0) + 1
        per_source[row.source] = per_source.get(row.source, 0) + 1
        results.append(Passage(title=row.title, heading=row.heading, source=row.source,
                               url=row.url, path=row.path, text=row.body, score=-row.rank))
        if len(results) >= k:
            break
    return tuple(results)


def _truncate(text: str, limit: int) -> str:
    """Cut text to limit characters at a word boundary, marking the cut."""
    if len(text) <= limit:
        return text
    cut = text[:max(limit - 2, 0)]
    space = cut.rfind(' ')
    if space > limit * 0.5:
        cut = cut[:space]
    return cut.rstrip() + ' …'


def fit_passages(passages: Sequence[Passage], limit: int = PASSAGE_CHAR_LIMIT) -> tuple[Passage, ...]:
    """Return copies of the passages that fit in limit characters (first one always kept)."""
    kept: list[Passage] = []
    used = 0
    for passage in passages:
        text = passage.text
        if not kept:
            text = _truncate(text, limit)
        elif used + len(text) > limit:
            break
        used += len(text)
        kept.append(dataclasses.replace(passage, text=text))
    return tuple(kept)


def passage_char_budget(num_ctx: int, num_predict: int, prompt_estimate_tokens: int) -> int:
    """Characters of passage text that fit the context window of one call, or 0 for none.

    ``min(9000, 3 * (num_ctx - num_predict - estimate(system + history + prompt) - 300))``
    using the ``num_ctx`` actually passed for the call.
    """
    free_tokens = num_ctx - num_predict - prompt_estimate_tokens - CONTEXT_SAFETY_TOKENS
    return max(0, min(PASSAGE_CHAR_LIMIT, CONTEXT_RESERVE_CHARS_PER_TOKEN * free_tokens))
