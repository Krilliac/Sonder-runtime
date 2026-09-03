"""Pure serve-target selection policy.

A served call either names its own target or takes the default route, and
only an operator-selected cloud tier may use the documented availability
fallback; an exact user-supplied model selector must never be replaced.
Moved from ``server.py`` in the WP1 Three-Hundred-Sixteenth Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations


def allow_cloud_fallback_for_target(tier_label):
    """Whether an availability fallback may replace this resolved target.

    A configured cloud *tier* is an operator-selected route and can use its
    documented K3-to-K2.7 availability fallback. A ``model:<name>`` label came
    from an exact user-supplied live-catalog selector, so it must never spend
    tokens on, or return a response from, a different model.
    """
    return not str(tier_label or "").casefold().startswith("model:")


def explicit_serve_selection(tier, model_override):
    """Whether a call names its own target instead of the default route."""
    if str(model_override or "").strip():
        return True
    return str(tier or "").strip().lower() not in ("", "sonder", "local")
