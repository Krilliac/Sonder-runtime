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
  outcome.  When ``repeat_limit`` consecutive attempts return an identical
  fingerprint, it reports a stall.  The caller's recovery must be materially
  different from "try the same thing again": the codegen loop keeps the best
  version so far, stops regenerating that file, and names the stall in its
  report so the operator (or a supervising agent) changes the spec, the
  error regex, or the approach.

A clean (empty) outcome is never a stall, and any change in the fingerprint
resets the streak, so a loop that is still moving is never cut short.
"""
from __future__ import annotations

import hashlib
import re


GUARD_NAME = "verification_no_progress"
MAX_VERIFICATION_ATTEMPTS = 6
DEFAULT_REPEAT_LIMIT = 2

_WHITESPACE = re.compile(r"\s+")


def bounded_attempts(value, default: int = 2) -> int:
    """Clamp a requested attempt count to ``1..MAX_VERIFICATION_ATTEMPTS``."""
    if isinstance(value, bool):
        value = default
    try:
        attempts = int(value)
    except (TypeError, ValueError):
        attempts = default
    return max(1, min(attempts, MAX_VERIFICATION_ATTEMPTS))


def outcome_fingerprint(errors) -> str:
    """Order-insensitive digest of a verifier's error lines.

    Whitespace runs are collapsed so reflowed output with the same content
    is recognised as the same outcome.  An empty outcome returns ``""``.
    """
    lines = sorted({
        _WHITESPACE.sub(" ", str(line)).strip()
        for line in (errors or ())
        if str(line).strip()
    })
    if not lines:
        return ""
    digest = hashlib.sha256("\n".join(lines).encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


class VerificationProgressGuard:
    """Detect ``repeat_limit`` consecutive identical failing outcomes."""

    def __init__(self, repeat_limit: int = DEFAULT_REPEAT_LIMIT):
        self.repeat_limit = max(2, int(repeat_limit))
        self._last = ""
        self._streak = 0
        self.stalled_fingerprint = ""

    def observe(self, errors) -> bool:
        """Record one attempt's outcome; return True when the loop stalled."""
        fingerprint = outcome_fingerprint(errors)
        if not fingerprint:
            self._last, self._streak = "", 0
            return False
        if fingerprint == self._last:
            self._streak += 1
        else:
            self._last, self._streak = fingerprint, 1
        if self._streak >= self.repeat_limit:
            self.stalled_fingerprint = fingerprint
            return True
        return False
