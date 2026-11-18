"""The safety classifier with the live permission catalog's risk grade filled in.

The rules are ``sonder_runtime.domain.security.tool_call_classifier``; this
module only reads the grade for the tool (``permission_policy.risk_of``) and
asks them, for callers outside the layered packages (``server.py`` and other
entry modules).  Inside ``sonder_runtime``, the restricted tool lane consults
the domain rules directly.  A grade that cannot be read is no grade, which the
rules answer with ``ASK``.  Stateless and thread-safe.
"""
from __future__ import annotations

from typing import Any

from sonder_runtime.domain.security import tool_call_classifier as _rules
from sonder_runtime.domain.security.tool_call_classifier import ALLOW, ASK, BLOCK, VERDICTS


def risk_of(tool_name: str) -> str:
    """The permission catalog's grade for ``tool_name``; ``""`` when it cannot be read."""
    try:
        from sonder_runtime.adapters.security.permission_policy import permission_policy

        return str(permission_policy.risk_of(tool_name))
    except Exception:  # noqa: BLE001 - an unreadable grade asks, never allows
        return ""


def classify(tool_name: str, arguments: Any) -> str:
    """``ALLOW``, ``ASK`` or ``BLOCK`` for one tool call; ``ASK`` on any fault."""
    return _rules.classify(tool_name, arguments, risk=risk_of(tool_name))


class SecurityClassifier:
    """Object form of :func:`classify`; every model is classified the same way.

    ``model_name`` is accepted so callers can pass what they know, but no model
    name exempts a call: an unrestricted model gets the same verdict.
    """

    def classify(self, model_name: str, tool_name: str, arguments: Any) -> str:
        del model_name
        return classify(tool_name, arguments)


__all__ = ["ALLOW", "ASK", "BLOCK", "VERDICTS", "SecurityClassifier", "classify", "risk_of"]
