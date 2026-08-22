"""Endpoint helpers for the standalone Sonder client."""

from __future__ import annotations

import os


DEFAULT_LOCAL_FALLBACK_SERVER = "http://127.0.0.1:11435"


def local_fallback_server(*, environ=None):
    """Return the configured local fallback endpoint."""
    values = os.environ if environ is None else environ
    return values.get("SONDER_LOCAL_FALLBACK", DEFAULT_LOCAL_FALLBACK_SERVER)


def same_server(first, second):
    """Compare endpoints while ignoring surrounding whitespace and slashes."""
    return (first or "").strip().rstrip("/") == (second or "").strip().rstrip("/")


__all__ = ["DEFAULT_LOCAL_FALLBACK_SERVER", "local_fallback_server", "same_server"]
