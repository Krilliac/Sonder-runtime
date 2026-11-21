"""Read-only listener configuration and reachability probe for the REPL."""
from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sonder_runtime.platform.config import SonderConfig


DEFAULT_HOST: str = os.environ.get("SONDER_HOST", "127.0.0.1")
DEFAULT_PORT: int = int(os.environ.get("SONDER_PORT", "11435"))


def configure_typed_config(config: SonderConfig) -> None:
    """Bind listener probing to validated typed server settings."""
    global DEFAULT_HOST, DEFAULT_PORT
    DEFAULT_HOST = config.server.host
    DEFAULT_PORT = config.server.port


def port_open(
    host: str | None = None,
    port: int | str | None = None,
    timeout: float = 0.5,
) -> bool:
    """Return whether the configured HTTP listener accepts a connection."""
    host = DEFAULT_HOST if host is None else host
    port = DEFAULT_PORT if port is None else port
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((connect_host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "configure_typed_config", "port_open"]
