"""Runtime guard: stop a verify/repair loop that has stopped making progress.

Issue #510 section 6 ("no-progress verification-loop guard").

A generate -> build -> score loop that keeps getting the *same* verifier
verdict back is not converging; every further attempt spends an expensive
model request (for the codegen ensemble, several) to reproduce a result the
host already has.  Two bounds apply:

* ``bounded_attempts`` clamps the caller's attempt count to
  ``MAX_VERIFICATION_ATTEMPTS``.  Before this guard ``codegen_build_loop``
  accepted any integer, so one tool call could request unbounded ensemble
  generations per file.
* :class:`VerificationProgressGuard` fingerprints each failing verifier
  outcome as a sorted multiset of normalized error lines (duplicates are
  kept, so fixing two of three copies of one error is not "identical").
  When ``repeat_limit`` consecutive observed attempts return an identical
  fingerprint and, when the caller supplies a score, the score did not
  improve, it reports a stall.  The caller's recovery must be materially
  different from "try the same thing again": the codegen loop keeps the best
  version so far, stops regenerating that file, and names the stall in its
  report so the operator (or a supervising agent) changes the spec, the
  error regex, or the approach.

A clean (empty) outcome is never a stall; a different fingerprint, an
improved score, or an explicit :meth:`VerificationProgressGuard.reset`
restarts the streak.  The guard only compares what it is shown: callers must
not feed it outcomes that cannot be compared (a placeholder standing in for
unmatched output, truncated output, or a masked count) and should call
``reset`` instead, so an unreadable measurement never counts toward a stall.
"""
from __future__ import annotations

import hashlib
import re


GUARD_NAME = "verification_no_progress"
MAX_VERIFICATION_ATTEMPTS = 6
DEFAULT_REPEAT_LIMIT = 2

_WHITESPACE = re.compile(r"\s+")


def requested_attempts(value, default: int = 2) -> int:
    """The integer a caller's attempt value means, before any clamping.

    Booleans and unparseable values fall back to ``default``; floats and
    numeric strings are truncated by ``int``.
    """
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bounded_attempts(value, default: int = 2) -> int:
    """Clamp a requested attempt count to ``1..MAX_VERIFICATION_ATTEMPTS``."""
    attempts = requested_attempts(value, default)
    return max(1, min(attempts, MAX_VERIFICATION_ATTEMPTS))


def outcome_fingerprint(errors) -> str:
    """Order-insensitive digest of a verifier's error lines.

    Lines are normalized (whitespace runs collapsed) and sorted as a list, so
    duplicates count: three copies of an error and one copy are different
    outcomes.  An empty outcome returns ``""``.
    """
    lines = sorted(
        _WHITESPACE.sub(" ", str(line)).strip()
        for line in (errors or ())
        if str(line).strip()
    )
    if not lines:
        return ""
    digest = hashlib.sha256("\n".join(lines).encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


class VerificationProgressGuard:
    """Detect ``repeat_limit`` consecutive identical, non-improving failures."""

    def __init__(self, repeat_limit: int = DEFAULT_REPEAT_LIMIT):
        self.repeat_limit = max(2, int(repeat_limit))
        self._last = ""
        self._last_score = None
        self._streak = 0
        self.stalled_fingerprint = ""

    def reset(self) -> None:
        """Forget the streak (an outcome that cannot be compared was seen)."""
        self._last, self._last_score, self._streak = "", None, 0

    def observe(self, errors, score=None) -> bool:
        """Record one attempt's outcome; return True when the loop stalled.

        ``score`` is optional and compared with ``<`` (lower is better, as in
        ``codegen_loop.score``); an improvement restarts the streak even when
        the fingerprint is unchanged.
        """
        fingerprint = outcome_fingerprint(errors)
        if not fingerprint:
            self.reset()
            return False
        improved = (
            score is not None
            and self._last_score is not None
            and score < self._last_score
        )
        if fingerprint == self._last and not improved:
            self._streak += 1
        else:
            self._last, self._streak = fingerprint, 1
        if score is not None:
            self._last_score = score
        if self._streak >= self.repeat_limit:
            self.stalled_fingerprint = fingerprint
            return True
        return False
