"""Composition point for the reference lookup.

Builds the read-only index adapter from the platform configuration (through
``reference_lookup_state``), applies the kill switch and the runtime-policy
rule, and offers what the callers need: ``applies`` (does automatic lookup
apply to a model), ``retrieve_for`` (the application-layer retrieval with the
index and switch filled in), ``search`` (the read-only ``reference_search``
tool) and ``status`` / ``status_line`` (for the policy status output and
``diagnostics``).

Nothing here raises to a caller: every failure becomes a typed not-applied
result with a reason, and the last such failure is kept for ``status``.  The
index path comes only from the platform configuration, never from the runtime
policy or a tool argument.  Thread-safe: all shared state lives in
``reference_lookup_state`` behind a lock; each search opens its own connection.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import sonder_runtime.adapters.reference_lookup_state as _state
import sonder_runtime.adapters.runtime_policy as _policy
from sonder_runtime.application.chat import reference_lookup as _lookup
from sonder_runtime.domain.reference_lookup import applies as _applies
from sonder_runtime.domain.reference_lookup.answer_check import format_sources
from sonder_runtime.domain.reference_lookup.prompt import build_prompt
from sonder_runtime.domain.reference_lookup.sanitize import (
    one_line,
    sanitize,
    terminal_text,
)
from sonder_runtime.domain.reference_lookup.selection import PASSAGE_CHAR_LIMIT

MIN_K = 1
MAX_K = 8
DEFAULT_K = 5
UNAVAILABLE_PREFIX = "reference lookup unavailable: "
_FAILED_REASONS = frozenset({_lookup.LookupReason.INDEX_UNAVAILABLE, _lookup.LookupReason.SEARCH_FAILED})


class SearchOutcome(str, Enum):
    FOUND = "found"
    NO_MATCH = "no_match"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SearchResult:
    """What ``search`` returns: the text for the tool plus a typed outcome."""

    outcome: SearchOutcome
    text: str
    reason: str = ""


def clamp_k(value: Any) -> int:
    """``k`` limited to 1..8; anything that is not a whole number is the default."""
    if isinstance(value, bool):
        return DEFAULT_K
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_K
    return max(MIN_K, min(MAX_K, number))


def _rule(policy: Mapping[str, Any] | None = None) -> _applies.AppliesRule:
    """The applies rule in force; built-in defaults when the policy cannot be read."""
    try:
        return _policy.reference_lookup_rule(policy if policy is not None else _policy.load(create=False))
    except Exception:  # noqa: BLE001  # lookup is a help, not a safeguard: defaults stay in force
        return _applies.AppliesRule.from_policy(None)


def applies(model: Any, policy: Mapping[str, Any] | None = None) -> _applies.AppliesDecision:
    """Whether automatic lookup applies to ``model`` under the current policy and kill switch."""
    try:
        return _applies.decide(model, _rule(policy), kill_switch_off=_state.kill_switch_off())
    except Exception:  # noqa: BLE001
        return _applies.AppliesDecision(False, _applies.AppliesReason.NO_MODEL)


def _remember(bundle: _lookup.Bundle) -> None:
    if bundle.reason in _FAILED_REASONS:
        _state.set_last_error(
            sanitize(f"{bundle.reason_text}: {bundle.detail}" if bundle.detail else bundle.reason_text)
        )
    elif bundle.applied:
        _state.set_last_error("")


def retrieve_for(
    question: str,
    expand_fn: Callable[[str], str] | None,
    k: int,
    char_budget: int,
) -> _lookup.Bundle:
    """Retrieve passages for a question with the configured index; never raises."""
    try:
        bundle = _lookup.retrieve(
            question, expand_fn, k, char_budget,
            index=_state.index(), enabled=not _state.kill_switch_off(),
        )
    except Exception as exc:  # noqa: BLE001  # fail open
        bundle = _lookup.Bundle(
            applied=False, reason=_lookup.LookupReason.SEARCH_FAILED,
            detail=sanitize(f"{type(exc).__name__}: {exc}"),
        )
    _remember(bundle)
    return bundle


def search(query: Any, k: Any = DEFAULT_K) -> SearchResult:
    """Search for the ``reference_search`` tool; never raises.

    Passages are framed with the lookup's marker lines (a fresh random code per
    call) so a calling model sees them as data, followed by a Sources list.
    """
    try:
        text = query if isinstance(query, str) else ""
        bundle = retrieve_for(text, None, clamp_k(k), PASSAGE_CHAR_LIMIT)
        if bundle.applied and bundle.passages:
            code = _lookup.new_code()
            body = build_prompt(text, bundle.passages, code) + "\n\n" + format_sources(bundle.passages)
            return SearchResult(SearchOutcome.FOUND, terminal_text(body))
        if bundle.reason is _lookup.LookupReason.NO_MATCH:
            return SearchResult(SearchOutcome.NO_MATCH, "no reference passages matched the query",
                                bundle.reason_text)
        reason = one_line(bundle.reason_text)
        return SearchResult(SearchOutcome.UNAVAILABLE, UNAVAILABLE_PREFIX + reason, reason)
    except Exception as exc:  # noqa: BLE001  # the tool reports, it never raises
        reason = sanitize(f"{type(exc).__name__}: {exc}")
        _state.set_last_error(reason)
        return SearchResult(SearchOutcome.UNAVAILABLE, UNAVAILABLE_PREFIX + reason, reason)


def status(policy: Mapping[str, Any] | None = None) -> _state.LookupStatus:
    """Index and rule status; never raises."""
    return _state.status(_rule(policy))


def status_line(policy: Mapping[str, Any] | None = None) -> str:
    """One line for the runtime policy status output and ``diagnostics``."""
    return _state.status_line(_rule(policy))


__all__ = [
    "DEFAULT_K", "MAX_K", "MIN_K", "SearchOutcome", "SearchResult",
    "applies", "clamp_k", "retrieve_for", "search", "status", "status_line",
]
