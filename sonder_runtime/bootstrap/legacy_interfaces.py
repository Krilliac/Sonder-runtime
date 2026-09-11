"""Composition boundary for the remaining legacy HTTP and REPL surfaces."""
from __future__ import annotations

import logging
from types import ModuleType

from .legacy_root import runtime_proxy


def legacy_runtime() -> ModuleType:
    """Resolve the concrete compatibility runtime at composition time.

    Keeping this lookup behind the existing interface boundary lets managed
    startup inject the exact runtime while preserving the root-server import
    ratchet and keeping tests able to replace the boundary explicitly.
    """
    from .legacy_root import runtime

    return runtime()

logger = logging.getLogger(__name__)


def configure_legacy_application(application) -> None:
    """Bind the owned Application through the existing interface bootstrap seam."""
    from .legacy_root import configure_application

    configure_application(application)


def require_inference_application(application):
    """Validate the legacy inference binding through the composition boundary."""
    from .legacy_root import require_inference_application as require

    return require(application)


def detach_owned_application(application) -> None:
    """Detach a closed owned graph from the legacy compatibility holder."""
    from .legacy_root import detach_owned_application as detach

    detach(application)


def configure_legacy_interfaces(runtime: ModuleType | None = None) -> None:
    """Inject the historical runtime into interfaces before they execute.

    The bootstrap layer is the only place that resolves the legacy root.  The
    HTTP and REPL modules remain importable and testable without importing it.
    """
    logger.debug(f"configure_legacy_interfaces: runtime_provided={runtime is not None}")
    import importlib

    serve = importlib.import_module("sonder_runtime.interfaces.http.serve")
    repl = importlib.import_module("sonder_runtime.interfaces.repl.repl")

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

    Routes through legacy_root (the only allowed server importer) to keep
    the architecture ratchet at 1 root module.
    """
    from .legacy_root import configure_capacity
    configure_capacity(
        autopilot_runs=autopilot_runs,
        fleet_workers=fleet_workers,
        training_jobs=training_jobs,
    )
    logger.info(
        f"legacy capacity configured: autopilot_runs={autopilot_runs}, "
        f"fleet_workers={fleet_workers}, training_jobs={training_jobs}"
    )


__all__ = [
    "configure_legacy_application",
    "configure_legacy_capacity",
    "configure_legacy_interfaces",
    "detach_owned_application",
    "legacy_runtime",
    "require_inference_application",
]
