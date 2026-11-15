"""Passage selection: identifier-title priority, same-identifier siblings, caps, size fitting.

Ported from the lookup client.  Everything here works on rows the adapter
already read; the store lookups it needs (title-restricted queries) are passed
in as callables.
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


# An ATT&CK-style identifier opening a title.  The shapes are real ATT&CK ones: a
# technique or ICS technique (T + 4 digits, optional .nnn), a mitigation (M + 4
# digits: M1xxx, ICS M08xx/M09xx), software (S + 4 digits), a group (G + 4 digits),
# a detection or analytic (DET/AN + 3 or 4 digits).  "M365 Defender ..." is a product, not an id.
_LEAD_ID_RE = re.compile(
    r'\s*((?:(?:DET|AN)\d{3,4}|[TMSG][01]\d{3})(?:\.\d{3})?)(?![A-Za-z0-9]|\.\d)', re.IGNORECASE)


def title_has(title: str | None, ident: str) -> bool:
    """True when ident is a whole token of title, case-insensitively.

    Not preceded or followed by a letter or digit, and not followed by a dot
    and a digit: "T1110" names the parent technique, so it does not match the
    title of sub-technique "T1110.002", a different page.
    """
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(ident) + r'(?![A-Za-z0-9]|\.\d)',
                     title or '', re.IGNORECASE) is not None


def lead_identifier(title: str | None) -> str | None:
    """The ATT&CK-style identifier a title opens with, lower-cased ("t1003.001"), or None."""
    match = _LEAD_ID_RE.match(title or '')
    return match.group(1).lower() if match else None


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


def _select_rows(rows: Sequence[RankedRow], k: int) -> list[RankedRow]:
    """The rows ``select_passages`` keeps, in order (the caps are documented there)."""
    if k <= 0:
        return []
    results: list[RankedRow] = []
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
        results.append(row)
        if len(results) >= k:
            break
    return results


def select_passages(rows: Sequence[RankedRow], k: int) -> tuple[Passage, ...]:
    """Return up to k best passages, best first.

    Caps: 2 passages per document, max(2, k - 2) per source, and passages whose
    normalised body equals an already selected one are skipped.  ``rows`` are
    taken in order, so priority rows placed first stay first.
    """
    return tuple(Passage(title=row.title, heading=row.heading, source=row.source,
                         url=row.url, path=row.path, text=row.body, score=-row.rank,
                         rowid=row.rowid)
                 for row in _select_rows(rows, k))


def with_siblings(
    rows: Sequence[RankedRow],
    k: int,
    fetch_title_rows: Callable[[str], Sequence[RankedRow]],
) -> list[RankedRow]:
    """``rows`` with each selected identifier page followed by the same page of the other sources.

    The k rows ``select_passages`` would keep are looked at in order.  When one
    of them has a title that opens with an ATT&CK-style identifier
    (``lead_identifier``), the best row of every other source whose title
    opens with the same identifier is placed right after it.  An emulation
    test or a detection rule titled "T1003.001 ..." ranks above the ATT&CK
    page "T1003.001 ..." because it repeats the question's words more often;
    this puts the page it names next to it.  Each identifier is handled once.

    ``fetch_title_rows(ident)`` is a title query: the best row of each source
    whose title holds ident, best first.  The result is the new order followed
    by all of ``rows``; pass it to ``select_passages``, whose caps apply as
    before (a moved row is not kept twice: its body is already seen).
    """
    ordered: list[RankedRow] = []
    done: set[str] = set()
    for row in _select_rows(rows, k):
        ordered.append(row)
        ident = lead_identifier(row.title)
        if ident is None or ident in done:
            continue
        done.add(ident)
        sources = {row.source}
        for other in fetch_title_rows(ident):
            if other.source not in sources and lead_identifier(other.title) == ident:
                sources.add(other.source)
                ordered.append(other)
    return ordered + list(rows)


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
