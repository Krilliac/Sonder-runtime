"""Pure campaign policy for recognizing host-toolchain failures."""

from __future__ import annotations


def environment_failure(output) -> bool:
    """Return whether a failed campaign attempt was caused by the host.

    A missing interpreter or compiler fails every attempt in that language and
    should not be recorded as a model failure or retried as a repair problem.
    """
    return str(output or "").startswith("missing runtime/compiler:")
