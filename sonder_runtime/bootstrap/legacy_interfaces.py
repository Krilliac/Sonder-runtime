"""Composition boundary for the remaining legacy HTTP and REPL surfaces."""
from __future__ import annotations

from types import ModuleType

from .legacy_root import runtime as legacy_runtime


def configure_legacy_interfaces(runtime: ModuleType | None = None) -> None:
    """Inject the historical runtime into interfaces before they execute.

    The bootstrap layer is the only place that resolves the legacy root.  The
    HTTP and REPL modules remain importable and testable without importing it.
    """
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    runtime = runtime or legacy_runtime()
    serve.configure_legacy_runtime(runtime)
    repl.configure_legacy_runtime(runtime)


__all__ = ["configure_legacy_interfaces"]
