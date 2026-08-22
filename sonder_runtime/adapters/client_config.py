"""Configuration resolution for the standalone Sonder client."""

from __future__ import annotations

import os


def parse_argv(argv):
    """Parse standalone-client ``--server`` and ``--key`` overrides."""
    server = None
    key = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--server" and i + 1 < len(argv):
            server = argv[i + 1]
            i += 2
        elif arg == "--key" and i + 1 < len(argv):
            key = argv[i + 1]
            i += 2
        else:
            i += 1
    return server, key


def resolve_config(argv, *, environ=None):
    """Resolve ``(server, api_key)`` from argv overrides and environment."""
    argv_server, argv_key = parse_argv(argv)
    values = os.environ if environ is None else environ
    server = argv_server or values.get("SONDER_SERVER", "")
    key = argv_key or values.get("SONDER_API_KEY", "")
    return server, key


__all__ = ["parse_argv", "resolve_config"]
