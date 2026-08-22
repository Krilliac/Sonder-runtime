"""Platform policy for the private chain-of-thought opt-in flag."""

from __future__ import annotations

import os


_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def opt_in_enabled(*, environ=None) -> bool:
    """Return whether the operator enabled the private-COT surface flag.

    The flag is only one half of the runtime's two-act gate; the permission
    rule is checked separately by the server. Keeping this environment-only
    decision here makes that distinction explicit and testable.
    """
    values = os.environ if environ is None else environ
    return str(values.get("SONDER_ALLOW_PRIVATE_COT", "")).strip().lower() in (
        _ENABLED_VALUES
    )


__all__ = ["opt_in_enabled"]
