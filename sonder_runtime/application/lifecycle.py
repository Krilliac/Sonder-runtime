"""Pure policies shared by application lifecycle entry points.

This module owns validation of the bounded context-size value accepted by
launcher start, stop, and restart requests. It has no process, transport,
configuration, or persistence concerns, so application boundaries can use it
without importing an adapter or the root launcher.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
import re

logger = logging.getLogger(__name__)


MAX_CONTEXT_TOKENS = 1_000_000
_CONTEXT_SIZE = re.compile(r"^(\d{1,7})(?:\.(\d{1,3}))?([km]?)$")


def process_state_number(tracker) -> int:
    """Return the stable numeric projection used by lifecycle metrics.

    The application boundary owns this presentation policy. It accepts the
    tracker protocol by shape so it does not depend on platform state types.
    """
    process = tracker.snapshot().process
    logger.debug(f"process_state_number: process={process.value!r}")
    if process.value == "degraded":
        logger.warning(f"process in degraded state")
    elif process.value == "recovery_required":
        logger.error(f"process requires recovery: state={process.value!r}")
        logger.warning(f"process requires recovery")
    elif process.value == "failed":
        logger.error(f"process in failed state: state={process.value!r}")
        logger.critical(f"runtime process has entered failed state, manual recovery required")
    return {
        "starting": 0,
        "migrating": 1,
        "ready": 2,
        "degraded": 3,
        "draining": 4,
        "stopping": 5,
        "failed": 6,
        "recovery_required": 7,
    }[process.value]


def normalize_context_size(value):
    """Validate the bounded context syntax accepted by lifecycle requests."""
    logger.debug(f"normalize_context_size: value={value!r}")
    if value is None or str(value).strip() == "":
        logger.warning("no context_size specified, falling back to default 8192")
    text = str(value or "8192").strip().lower()
    match = _CONTEXT_SIZE.fullmatch(text)
    if not match:
        raise ValueError("invalid context_size")
    try:
        number = Decimal(
            match.group(1) + ("." + match.group(2) if match.group(2) else "")
        )
    except InvalidOperation as exc:  # Defensive: the regular expression is stricter.
        raise ValueError("invalid context_size") from exc
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(3)]
    tokens = number * multiplier
    if tokens < 1 or tokens > MAX_CONTEXT_TOKENS:
        raise ValueError(
            "context_size must resolve to between 1 and %s tokens"
            % MAX_CONTEXT_TOKENS
        )
    if tokens != tokens.to_integral_value():
        raise ValueError("context_size must resolve to a whole number of tokens")
    return text
