"""Platform policy for the standalone client's local fallback flag."""

from __future__ import annotations

import os


_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def enabled(*, environ=None) -> bool:
    """Return whether standalone-client local fallback is enabled.

    The policy is default-on for backward compatibility.  Accepting an
    injectable environment keeps this boundary deterministic without making
    the packaged policy depend on the client process at import time.
    """
    values = os.environ if environ is None else environ
    return str(values.get("SONDER_FALLBACK_LOCAL", "1")).strip().lower() not in _DISABLED_VALUES


__all__ = ["enabled"]
