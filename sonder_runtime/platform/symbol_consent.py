"""Operator consent for symbol-server egress, and the operator's symbol stores.

Symbol servers (the Microsoft public server, a distro debuginfod, a studio
symstore share or HTTPS store) are egress: a debugger asked for symbols sends
module names, GUIDs and build ids to the server, and a UNC store is an SMB
connection. Consent therefore comes only from the operator:

- ``SONDER_SYMBOL_SERVER_CONSENT=1`` in the runtime's environment, or
- ``/crash symbols on`` at the attended console (per principal, this
  process only).

Stores come only from ``SONDER_SYMBOL_STORES`` -- a ``|``-separated list of
at most four https URLs or UNC shares -- and never from a tool argument. The
planner validates each entry lexically (``domain.debugging.symbol_path``) and
still requires the console confirmation before any of them is used.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Mapping

CONSENT_ENV = "SONDER_SYMBOL_SERVER_CONSENT"
STORES_ENV = "SONDER_SYMBOL_STORES"
MAX_STORES = 4
MAX_STORE_CHARS = 512
_TRUE = frozenset({"1", "true", "yes", "on"})
# Modes in which no egress is ever allowed. ``readonly`` is not a permission
# mode today; it is listed so a future read-only mode fails closed here.
_NETWORK_REFUSED_MODES = frozenset({"plan", "readonly"})


def symbol_server_consent(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(CONSENT_ENV, "")).strip().lower() in _TRUE


def operator_symbol_stores(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Raw operator stores (at most four); lexical validation is the planner's."""
    env = os.environ if environ is None else environ
    raw = str(env.get(STORES_ENV, "") or "")
    stores: list[str] = []
    for item in raw.split("|"):
        text = item.strip()
        if not text or len(text) > MAX_STORE_CHARS or "\x00" in text:
            continue
        if text not in stores:
            stores.append(text)
        if len(stores) >= MAX_STORES:
            break
    return tuple(stores)


class SymbolConsentState:
    """The ``SymbolConsent`` port: environment consent plus the session switch."""

    def __init__(self, *, environ: Callable[[], Mapping[str, str]] | None = None,
                 mode: Callable[[], str] | None = None) -> None:
        self._environ = environ or (lambda: os.environ)
        self._mode = mode
        self._session: dict[str, bool] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(context: Any) -> str:
        return str(getattr(context, "principal_id", "") or "")

    def allowed(self, context: Any) -> bool:
        if symbol_server_consent(self._environ()):
            return True
        with self._lock:
            return bool(self._session.get(self._key(context), False))

    def set_session(self, context: Any, allowed: bool) -> None:
        with self._lock:
            if allowed:
                self._session[self._key(context)] = True
            else:
                self._session.pop(self._key(context), None)

    def stores(self) -> tuple[str, ...]:
        return operator_symbol_stores(self._environ())

    def mode_permits_network(self) -> bool:
        if self._mode is None:
            return True
        try:
            mode = str(self._mode() or "")
        except Exception:
            return False  # a blind mode read fails closed
        return mode not in _NETWORK_REFUSED_MODES


__all__ = [
    "CONSENT_ENV", "MAX_STORES", "STORES_ENV", "SymbolConsentState",
    "operator_symbol_stores", "symbol_server_consent",
]
