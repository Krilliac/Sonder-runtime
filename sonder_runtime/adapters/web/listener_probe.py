"""Read-only listener configuration and reachability probe for the REPL."""
from __future__ import annotations

import os
import socket


DEFAULT_HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SONDER_PORT", "11435"))


def port_open(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=0.5) -> bool:
    """Return whether the configured HTTP listener accepts a connection."""
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((connect_host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "port_open"]
