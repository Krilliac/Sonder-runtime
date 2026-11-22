"""Health-probe outcome rules for the launcher's managed-port check.

``foreign_listener`` is reserved for a definite answer that is not the managed
runtime's proof. ``indeterminate`` means the port accepted a connection but no
answer arrived (timeout, reset or another transport error), as happens while
the managed runtime is bound but not yet serving; callers poll again instead of
treating it as a foreign owner.
"""
from __future__ import annotations

import time
import urllib.error
from typing import Callable

FOREIGN_LISTENER = "foreign_listener"
INDETERMINATE = "indeterminate"
# How long the launcher re-probes an unanswered port before an action decides.
INITIAL_PROBE_GRACE_SECONDS = 2.0


def probe_error_state(error: BaseException) -> str:
    """Classify an exception raised while sending the probe or reading its reply.

    An ``HTTPError`` is a definite non-2xx HTTP answer, so ``foreign_listener``.
    Any other ``OSError`` (``URLError``, timeouts, resets) carried no answer,
    so ``indeterminate``. Anything else (``ValueError``) stays
    ``foreign_listener``.
    """
    if isinstance(error, urllib.error.HTTPError):
        return FOREIGN_LISTENER
    if isinstance(error, OSError):
        return INDETERMINATE
    return FOREIGN_LISTENER


def settle_probe(
    probe: Callable[[], str],
    interval: float,
    grace: float | None = None,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return ``probe()``, re-run every ``interval`` while it is indeterminate.

    Re-probing stops after ``grace`` seconds (``INITIAL_PROBE_GRACE_SECONDS``
    when ``None``); the last state is returned, so a port that never answers
    is still ``indeterminate`` and the caller decides how to treat it.
    """
    limit = INITIAL_PROBE_GRACE_SECONDS if grace is None else float(grace)
    state = probe()
    deadline = clock() + limit
    while state == INDETERMINATE:
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
        state = probe()
    return state
