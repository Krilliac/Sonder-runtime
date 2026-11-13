"""Reference lookup for a chat turn: retrieve passages, frame the user turn, clean the reply.

The index is injected (an adapter implementing ``ReferenceSearch``), so this
module reads no files, opens no database and reads no environment.  Every
failure to look something up becomes a ``Bundle`` with ``applied=False`` and a
typed reason; the turn then proceeds as a normal bare answer.  The one
exception is cancellation: a cancelled search-terms call is re-raised, so the
turn stops instead of going on to the answer call.
"""
from __future__ import annotations

import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from ...domain.common.errors import Cancelled
from ...domain.reference_lookup.answer_check import (
    ECHO_WARNING,
    cited_numbers,
    missing_numbers,
    missing_warning,
)
from ...domain.reference_lookup.models import (
    IndexState,
    IndexStatus,
    Passage,
    RankedRow,
    ReferenceIndexError,
    TermStats,
)
from ...domain.reference_lookup.prompt import build_prompt, strip_markers
from ...domain.reference_lookup.relevance import best_coverage, term_weight
from ...domain.reference_lookup.sanitize import sanitize, terminal_text
from ...domain.reference_lookup.selection import (
    candidate_limit,
    fit_passages,
    select_passages,
)
from ...domain.reference_lookup.terms import (
    MAX_IDENTIFIERS,
    build_expand_prompt,
    filter_terms,
    fts_query,
    ordered_identifiers,
    parse_terms,
    query_terms,
)

NO_ROOM_WARNING = 'no room for references'
NO_MATCH_WARNING = 'no reference passages matched the question'
LOW_RELEVANCE_WARNING = 'no reference passage was close enough to the question'
NO_TERMS_WARNING = 'keyword expansion returned no usable terms'
EXPANSION_FAILED_PREFIX = 'keyword expansion failed: '
CODE_BYTES = 4
# The model-call failure kind a cancelled call carries (``ModelCallError.kind``).
CANCELLED_KIND = "cancelled"


class ReferenceSearch(Protocol):
    """What this module needs from the index adapter."""

    def status(self) -> IndexStatus: ...

    def search_rows(self, match: str, limit: int,
                    idents: Sequence[str] = ()) -> tuple[RankedRow, ...]: ...


class TermStatistics(Protocol):
    """What the relevance floor needs from the index adapter.

    Kept apart from ``ReferenceSearch``: an index that does not offer it is
    searched as before, without the floor.
    """

    def term_stats(self, terms: Sequence[str], rowids: Sequence[int]) -> TermStats: ...


class LookupReason(str, Enum):
    """Why a turn did or did not get passages."""

    APPLIED = "applied"
    DISABLED = "disabled"
    EMPTY_QUESTION = "empty_question"
    INDEX_UNAVAILABLE = "index_unavailable"
    SEARCH_FAILED = "search_failed"
    NO_MATCH = "no_match"
    LOW_RELEVANCE = "low_relevance"
    NO_ROOM = "no_room"


@dataclass(frozen=True)
class Bundle:
    """What retrieval found for one question; ``applied`` is False for a bare answer."""

    applied: bool
    reason: LookupReason
    passages: tuple[Passage, ...] = ()
    terms: tuple[str, ...] = ()
    dropped_terms: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    index_state: IndexState | None = None
    detail: str = ""

    @property
    def reason_text(self) -> str:
        """The short reason for the 'reference lookup unavailable' line and receipts."""
        if self.reason is LookupReason.INDEX_UNAVAILABLE and self.index_state is not None:
            return self.index_state.value
        return self.reason.value


@dataclass(frozen=True)
class ReplyReview:
    """A model reply after cleaning, with the citation check."""

    text: str
    echoed_marker: bool
    cited: frozenset[int]
    missing: tuple[int, ...]
    warnings: tuple[str, ...]


def new_code() -> str:
    """A fresh random marker code for one turn."""
    return secrets.token_hex(CODE_BYTES)


def _bare(reason: LookupReason, *, terms: Sequence[str] = (), dropped: Sequence[str] = (),
          warnings: Sequence[str] = (), state: IndexState | None = None,
          detail: str = "") -> Bundle:
    return Bundle(applied=False, reason=reason, terms=tuple(terms), dropped_terms=tuple(dropped),
                  warnings=tuple(warnings), index_state=state, detail=detail)


def is_cancellation(exc: BaseException) -> bool:
    """True for a cancelled model call: the domain ``Cancelled`` or a transport error of that kind."""
    return isinstance(exc, Cancelled) or getattr(exc, "kind", None) == CANCELLED_KIND


def _expand(question: str, expand_fn: Callable[[str], str] | None
            ) -> tuple[list[str], list[str], list[str]]:
    """(kept terms, dropped terms, warnings); a failed or empty expansion is a warning only.

    A cancelled call is re-raised: cancellation ends the turn, it is not a
    search problem to work around.
    """
    if expand_fn is None:
        return [], [], []
    try:
        terms = parse_terms(expand_fn(build_expand_prompt(question)))
    except Exception as exc:  # reported to the caller as a warning, never hidden
        if is_cancellation(exc):
            raise
        return [], [], [EXPANSION_FAILED_PREFIX + sanitize(exc)]
    kept, dropped = filter_terms(question, terms)
    if not kept:
        return [], dropped, [NO_TERMS_WARNING]
    return kept, dropped, []


def passage_coverage(index: object, terms: Sequence[str], passages: Sequence[Passage]) -> float | None:
    """How much of the query's vocabulary the best of ``passages`` holds (0..1), or None.

    None means the measure is not available (the index does not offer term
    statistics, a passage has no row id, or the index could not be read): the
    caller then keeps the passages, as before the floor existed.
    """
    stats_for = getattr(index, "term_stats", None)
    rowids = [passage.rowid for passage in passages]
    if not callable(stats_for) or not terms or not rowids or any(row <= 0 for row in rowids):
        return None
    try:
        stats = stats_for(terms, rowids)
        weights = {term: term_weight(stats.passage_count, stats.matching.get(term, 0))
                   for term in terms}
        return best_coverage(weights, (stats.held.get(row, frozenset()) for row in rowids))
    except Exception:  # noqa: BLE001  # the floor is a help: without it the passages are kept
        return None


def retrieve(
    question: str,
    expand_fn: Callable[[str], str] | None,
    k: int,
    char_budget: int,
    *,
    index: ReferenceSearch,
    enabled: bool = True,
    min_coverage: float = 0.0,
) -> Bundle:
    """Search the index for passages that fit ``char_budget`` characters.

    ``expand_fn(prompt)`` asks the answering model for search terms and returns
    its text (the caller sets temperature 0, num_predict 80 and no system
    prompt); ``None`` skips expansion.  A failed call searches with the
    question words alone and adds a warning.  Only the first
    ``MAX_IDENTIFIERS`` identifiers of the question get title priority (one
    title query each).

    ``min_coverage`` above 0 is the relevance floor for passages handed to a
    model on its own (``domain.reference_lookup.relevance``): when the best
    passage holds less than that share of the query's vocabulary, the turn
    gets none (``LookupReason.LOW_RELEVANCE``).  The default, 0, is a plain
    search that returns the nearest passages whatever they are.

    Never raises, except to pass on a cancelled search-terms call
    (``is_cancellation``).
    """
    try:
        if not enabled:
            return _bare(LookupReason.DISABLED)
        if not isinstance(question, str) or not question.strip():
            return _bare(LookupReason.EMPTY_QUESTION)
        status = index.status()
        if not status.ready:
            return _bare(LookupReason.INDEX_UNAVAILABLE, state=status.state, detail=status.detail)
        if char_budget <= 0:
            return _bare(LookupReason.NO_ROOM, warnings=(NO_ROOM_WARNING,))
        terms, dropped, warnings = _expand(question, expand_fn)
        match = fts_query(question, terms)
        rows: tuple[RankedRow, ...] = ()
        if match and k > 0:
            try:
                rows = index.search_rows(match, candidate_limit(k),
                                         ordered_identifiers(question, MAX_IDENTIFIERS))
            except ReferenceIndexError as exc:
                return _bare(LookupReason.INDEX_UNAVAILABLE, terms=terms, dropped=dropped,
                             warnings=warnings, state=exc.state, detail=exc.detail)
        found = select_passages(rows, k)
        passages = fit_passages(found, char_budget)
        if not found:
            warnings.append(NO_MATCH_WARNING)
            return _bare(LookupReason.NO_MATCH, terms=terms, dropped=dropped, warnings=warnings,
                         state=IndexState.READY)
        if min_coverage > 0:
            covered = passage_coverage(index, query_terms(question, terms), passages)
            if covered is not None and covered < min_coverage:
                warnings.append(LOW_RELEVANCE_WARNING)
                return _bare(LookupReason.LOW_RELEVANCE, terms=terms, dropped=dropped,
                             warnings=warnings, state=IndexState.READY,
                             detail=f'best passage coverage {covered:.2f} is under {min_coverage:.2f}')
        if len(passages) < len(found):
            warnings.append(f'{len(found) - len(passages)} passage(s) left out to fit the prompt size')
        return Bundle(applied=True, reason=LookupReason.APPLIED, passages=passages,
                      terms=tuple(terms), dropped_terms=tuple(dropped), warnings=tuple(warnings),
                      index_state=IndexState.READY)
    except Exception as exc:  # fail open: a lookup problem must never break the turn
        if is_cancellation(exc):
            raise
        return _bare(LookupReason.SEARCH_FAILED, detail=sanitize(f'{type(exc).__name__}: {exc}'))


def review_reply(bundle: Bundle, reply: str) -> ReplyReview:
    """Clean a reply (display-safe text, copied marker lines removed) and check its citations.

    Marker lines are removed by shape, whatever code they carry, exactly as in
    the client.
    """
    text, echoed = strip_markers(terminal_text(reply))
    warnings: list[str] = [ECHO_WARNING] if echoed else []
    cited = cited_numbers(text)
    missing = missing_numbers(cited, len(bundle.passages))
    if missing:
        warnings.append(missing_warning(missing))
    return ReplyReview(text=text, echoed_marker=echoed, cited=cited, missing=missing,
                       warnings=tuple(warnings))


def wrap_generate(gen: Callable[..., Any], bundle: Bundle, code: str) -> Callable[..., Any]:
    """Wrap ``gen(prompt, history, ...)`` so passages frame the user turn.

    The wrapper sends the framed prompt and returns the cleaned reply, so
    callers store and show clean text while the raw question stays the stored
    task.  A bundle without passages, or an empty code, returns ``gen`` as is.
    A reply that is not text is returned unchanged.
    """
    if not bundle.applied or not bundle.passages or not code:
        return gen

    def generate(prompt: Any, *args: Any, **kwargs: Any) -> Any:
        framed = build_prompt(prompt, bundle.passages, code) if isinstance(prompt, str) else prompt
        reply = gen(framed, *args, **kwargs)
        if not isinstance(reply, str):
            return reply
        return review_reply(bundle, reply).text

    return generate
