"""Bounded guard for unchanged failed agent tool retries.

This guard sits on the host agent loop's tool-dispatch boundary.  It is
deliberately separate from transport retry policy: model/provider retries and
idempotent reads are allowed to keep their own contracts.  The guard only
counts a failed host observation after the dispatcher has returned it.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                      separators=(",", ":"))


def failure_fingerprint(*, tool: str, resource: Any, scope: Any,
                        arguments: Any, outcome: Any) -> str:
    """Hash the complete failed call identity and its returned outcome."""
    payload = {
        "tool": str(tool),
        "resource": resource,
        "scope": scope,
        "arguments": arguments,
        "outcome": str(outcome),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RetryDecision:
    allowed: bool
    attempts: int
    reason: str = ""


class FailedToolRetryGuard:
    """Stop an unchanged failed host call after a small bounded allowance.

    State is intentionally per agent run.  A successful call clears the same
    call identity, and a changed call identity starts a fresh allowance.  The
    failed outcome participates in the recorded fingerprint, so changed host
    errors are distinguishable and can reset the bounded recovery window.
    """

    def __init__(self, *, max_retries: int = 2) -> None:
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        self.max_retries = max_retries
        self._failures: dict[str, tuple[int, str, str]] = {}

    def decision(self, identity: str) -> RetryDecision:
        entry = self._failures.get(identity)
        if entry is None:
            return RetryDecision(True, 0)
        attempts, _fingerprint, preview = entry
        if attempts >= self.max_retries:
            return RetryDecision(
                False,
                attempts,
                "unchanged failed tool call is bounded after %d attempts (%s)"
                % (attempts, preview),
            )
        return RetryDecision(True, attempts)

    def record_failure(self, identity: str, *, tool: str, resource: Any,
                       scope: Any, arguments: Any, outcome: Any) -> int:
        fingerprint = failure_fingerprint(
            tool=tool, resource=resource, scope=scope,
            arguments=arguments, outcome=outcome,
        )
        previous = self._failures.get(identity)
        attempts = previous[0] + 1 if previous and previous[1] == fingerprint else 1
        self._failures[identity] = (
            attempts,
            fingerprint,
            str(outcome).replace("\n", " ")[:240],
        )
        return attempts

    def record_success(self, identity: str) -> None:
        self._failures.pop(identity, None)

    def record_blocked(self, identity: str) -> int:
        """Advance the synthetic bounded observation without changing its cause."""
        entry = self._failures.get(identity)
        if entry is None:
            return 0
        attempts, fingerprint, preview = entry
        attempts += 1
        self._failures[identity] = (attempts, fingerprint, preview)
        return attempts

    def clear_except(self, identity: str | None = None) -> None:
        if identity is None:
            self._failures.clear()
            return
        entry = self._failures.get(identity)
        self._failures.clear()
        if entry is not None:
            self._failures[identity] = entry
