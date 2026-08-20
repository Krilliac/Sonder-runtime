"""Composition boundary for the remaining legacy HTTP and REPL surfaces."""
from __future__ import annotations


def configure_legacy_interfaces() -> None:
    """Inject the historical runtime into interfaces before they execute.

    The bootstrap layer is the only place that resolves the legacy root.  The
    HTTP and REPL modules remain importable and testable without importing it.
    """
    import server
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    serve.configure_legacy_runtime(server)
    repl.configure_legacy_runtime(server)


__all__ = ["configure_legacy_interfaces"]
