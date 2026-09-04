"""Composition boundary for the remaining legacy HTTP and REPL surfaces."""
from __future__ import annotations

import logging
from types import ModuleType

from .legacy_root import runtime as legacy_runtime, runtime_proxy

logger = logging.getLogger(__name__)


def configure_legacy_interfaces(runtime: ModuleType | None = None) -> None:
    """Inject the historical runtime into interfaces before they execute.

    The bootstrap layer is the only place that resolves the legacy root.  The
    HTTP and REPL modules remain importable and testable without importing it.
    """
    logger.debug(f"configure_legacy_interfaces: runtime_provided={runtime is not None}")
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    runtime = runtime or runtime_proxy()
    logger.warning("legacy HTTP/REPL interfaces still in use, deprecated code path exercised")
    logger.debug("injecting legacy runtime into HTTP and REPL interfaces")
    serve.configure_legacy_runtime(runtime)
    repl.configure_legacy_runtime(runtime)
    logger.info("legacy HTTP and REPL interfaces configured")
    logger.debug("legacy interfaces configured")


__all__ = ["configure_legacy_interfaces"]
