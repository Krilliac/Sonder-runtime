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


def configure_legacy_capacity(
    *,
    autopilot_runs: int,
    fleet_workers: int,
    training_jobs: int,
) -> None:
    """Push typed capacity limits into legacy modules.

    All three limits route through server.py (the only allowed root legacy
    module) which delegates to master_orchestrator and adaptive_training
    internally, keeping the architecture ratchet at 1 root module.
    """
    import server as legacy_server
    legacy_server.configure_capacity(
        autopilot_runs=autopilot_runs,
        fleet_workers=fleet_workers,
        training_jobs=training_jobs,
    )
    logger.info(
        f"legacy capacity configured: autopilot_runs={autopilot_runs}, "
        f"fleet_workers={fleet_workers}, training_jobs={training_jobs}"
    )


__all__ = ["configure_legacy_capacity", "configure_legacy_interfaces"]
