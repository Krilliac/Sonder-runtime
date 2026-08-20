"""Parsing policy for environment-backed runtime options."""

import os


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
