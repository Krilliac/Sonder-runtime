"""Load the default chat model as soon as the HTTP listener is bound.

Without this the first chat after ``serve`` starts pays the whole cold load. The
serving entry point calls :func:`start_default_model_prewarm` once, right after
the listener is bound. It starts one daemon thread that calls the runtime's
existing single-flight ``prewarm_model("")`` (the same call an empty-model chat
request makes), so tier resolution, the cloud / other-provider skip, the prewarm
gate and GPU admission are exactly the normal ones: nothing here lifts an
admission floor or loads a model by another route.

``prewarm_model`` only *starts* the weight load on its own worker and returns,
so the load's outcome is reported by that worker through :func:`load_finished`
and :func:`load_failed` (one log line each, admission refusals with their typed
reason).

Threads: the starter thread never raises and never blocks startup; a failure to
create it is logged. Nothing here owns shared state beyond the logger.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from sonder_runtime.adapters.inference.gpu_admission import GpuAdmissionRefused

logger = logging.getLogger("sonder.prewarm")


def load_finished(model: str, *, announce: bool = True) -> None:
    """The prewarm worker's load request completed.

    ``announce`` is true for the start-up prewarm (INFO); the per-request
    prewarm of every chat logs at DEBUG so it adds no line per chat.
    """
    logger.log(logging.INFO if announce else logging.DEBUG, "prewarm: %s loaded", model)


def load_failed(model: str, error: BaseException, *, announce: bool = True) -> None:
    """The prewarm worker's load request failed; admission refusals say why."""
    level = logging.WARNING if announce else logging.DEBUG
    if isinstance(error, GpuAdmissionRefused):
        logger.log(level, "prewarm: %s refused by GPU admission (%s): %s", model,
                   error.refusal.reason.value, error.refusal.detail)
    else:
        logger.log(level, "prewarm: %s not loaded (%s)", model, type(error).__name__)


def run_default_model_prewarm(prewarm: Callable[[str], bool],
                              enabled: Callable[[], bool]) -> str:
    """Start the default model's prewarm; return the outcome word that was logged.

    Never raises. ``started`` means the load was handed to its worker (its own
    result is logged later); ``skipped`` means there was nothing to load.
    """
    try:
        if not enabled():
            logger.info("prewarm: skipped at start (prewarm is disabled)")
            return "disabled"
        if prewarm(""):
            logger.info("prewarm: default chat model load started")
            return "started"
        logger.info("prewarm: skipped at start (no local default model to load, "
                    "a cloud or other-provider tier, or a load already in flight)")
        return "skipped"
    except Exception as error:
        logger.warning("prewarm: start-up prewarm failed (%s)", type(error).__name__,
                       exc_info=True)
        return "failed"


def start_default_model_prewarm(
    *, setting: bool, prewarm: Callable[[str], bool], enabled: Callable[[], bool],
    thread_factory: Callable[..., Any],
) -> bool:
    """Spawn the starter thread when ``setting`` is on; ``True`` if it was started.

    ``setting`` is ``[server].prewarm_on_start``; ``enabled`` is the global
    prewarm switch (``sonder_speculation.prewarm_enabled``), which also governs
    per-request prewarm and is off under the test suite.
    """
    if not setting:
        logger.info("prewarm: start-up prewarm is off ([server].prewarm_on_start)")
        return False
    try:
        thread = thread_factory(
            target=run_default_model_prewarm, args=(prewarm, enabled),
            name="sonder-startup-prewarm", daemon=True,
        )
        thread.start()
    except Exception:
        logger.warning("prewarm: could not start the start-up prewarm thread", exc_info=True)
        return False
    return True


__all__ = [
    "load_failed", "load_finished", "run_default_model_prewarm",
    "start_default_model_prewarm",
]
