"""Policies for environment-backed runtime options."""

import os


def cpu_thread_default(*, cpu_count=None):
    """Return the safe default worker count for local model requests.

    ``cpu_count`` is injectable for deterministic callers and tests; when it
    is omitted the host's current CPU count is used.  A missing or invalid
    host count still produces a usable single-thread minimum.
    """
    count = os.cpu_count() if cpu_count is None else cpu_count
    return max(1, count or 4)


def env_int_option(name, default=None, *, environ=None):
    """Return an integer environment option with the historical fallbacks."""
    values = environ if environ is not None else os.environ
    raw = values.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw.lower() in ("", "auto", "default", "none", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        return default
