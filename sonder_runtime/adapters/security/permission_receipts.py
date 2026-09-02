"""Durable, content-free receipts for unattended permission decisions.

``permission_modes.decide`` is pure and cannot open a database, so the
composition root installs this observer instead. Every unattended decision
worth a receipt -- a refusal, or an allow of anything but a ``safe`` read --
becomes one ``permission.unattended_refusal`` or ``permission.unattended_allow``
event on the application event sink, whose durable authority is
``operations.db``. The payload names the tool, surface, mode, risk, source and
action; it never carries arguments, paths, prompts, or output, so it can be
read back by anyone allowed to read the operations store.

Two installers exist because the decider can run before the application graph
does. ``install_default`` (called when the legacy server module loads) routes
receipts straight to the operations store; ``install`` (called by
``bootstrap.build_application``) replaces that with the application's own sink,
which decorates the same store with the local inspection ring.
"""
from __future__ import annotations

import importlib
import threading
from typing import Callable

REFUSAL_EVENT = "permission.unattended_refusal"
ALLOW_EVENT = "permission.unattended_allow"

_LOCK = threading.Lock()
_STATE: dict = {"provider": None, "observer": None, "default": False}


def _engine():
    return importlib.import_module("permission_modes")


def emit(sink, decision, surface: str) -> None:
    """Write one receipt for ``decision`` to ``sink`` (an ``EventSink``)."""
    if sink is None:
        return
    refused = decision.action == "deny"
    surface = str(surface or "unspecified")
    detail = {
        "category": "permission",
        "tool": decision.tool,
        "surface": surface,
        "mode": decision.mode,
        "risk": decision.risk,
        "source": decision.source,
        "action": decision.action,
    }
    sink.emit(
        REFUSAL_EVENT if refused else ALLOW_EVENT,
        summary="%s %s via %s (mode=%s, risk=%s, source=%s)" % (
            "refused" if refused else "allowed", decision.tool or "(empty name)",
            surface, decision.mode, decision.risk, decision.source,
        ),
        detail=detail,
        severity="WARNING" if refused else "INFO",
    )


def install(sink_provider: Callable[[], object]) -> None:
    """Route unattended decisions to ``sink_provider()``; replaces any earlier install."""
    if not callable(sink_provider):
        raise TypeError("sink_provider must be callable")
    engine = _engine()

    def observer(decision, surface):
        try:
            emit(sink_provider(), decision, surface)
        except Exception:
            # Receipts are evidence, never authority: a sink that cannot be
            # written must not change or block the decision.
            return

    with _LOCK:
        previous = _STATE["observer"]
        if previous is not None:
            engine.remove_decision_observer(previous)
        engine.add_decision_observer(observer)
        _STATE["provider"] = sink_provider
        _STATE["observer"] = observer
        _STATE["default"] = False


def install_default() -> None:
    """Install the durable operations sink when no application graph exists yet.

    A no-op when any observer is already installed, so a graph built earlier
    keeps its sink and a later legacy-module import cannot demote it.
    """
    with _LOCK:
        if _STATE["observer"] is not None:
            return
    holder: dict = {}

    def provider():
        if "sink" not in holder:
            from ..operations_event_sink import OperationsEventSink

            holder["sink"] = OperationsEventSink()
        return holder["sink"]

    install(provider)
    with _LOCK:
        _STATE["default"] = True


def uninstall() -> None:
    engine = _engine()
    with _LOCK:
        observer = _STATE["observer"]
        if observer is not None:
            engine.remove_decision_observer(observer)
        _STATE.update({"provider": None, "observer": None, "default": False})


def snapshot():
    """An opaque token for ``restore``: which sink is receiving receipts now."""
    with _LOCK:
        return (_STATE["provider"], _STATE["default"])


def restore(token) -> None:
    """Put back the sink ``snapshot`` saw, whichever kind it was, or none."""
    provider, default = token
    if provider is None:
        uninstall()
        return
    install(provider)
    if default:
        with _LOCK:
            _STATE["default"] = True


def installed() -> str:
    """``"application"``, ``"default"`` or ``""`` -- which sink is receiving receipts."""
    with _LOCK:
        if _STATE["observer"] is None:
            return ""
        return "default" if _STATE["default"] else "application"


__all__ = [
    "ALLOW_EVENT", "REFUSAL_EVENT", "emit", "install", "install_default",
    "installed", "restore", "snapshot", "uninstall",
]
