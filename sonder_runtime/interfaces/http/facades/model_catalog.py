"""Routing metadata for GET /v1/models rows (the additive ``sonder`` field).

GET /v1/sonder/ecosystem is administrator-only, so a non-administrator
client could not tell which provider serves a route.  Each OpenAI-shaped row
therefore carries::

    "sonder": {"kind": "route", "provider": "sonder_inference", "served_model": "qwen3:14b"}
    "sonder": {"kind": "model", "provider": "ollama"}

Only a provider id and a model id are exposed -- never an endpoint URL,
credential or health detail, which stay behind the admin route.

A route is resolved exactly as a chat turn resolves it: ``serve_target`` maps
the id to its rung label (``sonder`` follows the chat policy tier), and
``bridge_provider`` names the non-Ollama provider bound to that rung (None
for Ollama and hosted/cloud rungs).  A bridged rung's model comes from the
gateway's configuration-only ``served_tier_models()``; an Ollama rung's model
is the target itself.  Exact catalog models always run on Ollama.  Anything
that cannot be resolved reads as null rather than a guess, and nothing here
raises: this is display metadata on a listing route.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

ROUTE_FIELD = "sonder"
OLLAMA = "ollama"
_UNRESOLVED_LABELS = (None, "cloud-disabled")


def _served_tier_models(gateway: object) -> Mapping[str, Mapping[str, str]]:
    reporter = getattr(gateway, "served_tier_models", None)
    if not callable(reporter):
        return {}
    try:
        reported = reporter()
    except Exception:  # noqa: BLE001 - display metadata never fails the listing
        return {}
    return reported if isinstance(reported, Mapping) else {}


def _unknown_route() -> dict[str, Any]:
    return {"kind": "route", "provider": None, "served_model": None}


def _route_origin(route_id: str, serve_target: Callable, bridge_provider: Callable,
                  served: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    try:
        model, cloud, _augment, label = serve_target(route_id, None)
        if label in _UNRESOLVED_LABELS:
            return _unknown_route()
        provider = bridge_provider(label, cloud)
    except Exception:  # noqa: BLE001 - an unreadable policy or binding is unknown
        return _unknown_route()
    if provider is None:
        return {"kind": "route", "provider": OLLAMA,
                "served_model": model if isinstance(model, str) and model else None}
    tiers = served.get(str(provider))
    tier_model = tiers.get(str(label)) if isinstance(tiers, Mapping) else None
    return {"kind": "route", "provider": str(provider),
            "served_model": tier_model if isinstance(tier_model, str) and tier_model else None}


def annotate_model_rows(rows: list[dict[str, Any]], route_ids: Iterable[str], *,
                        serve_target: Callable, bridge_provider: Callable,
                        gateway: object) -> list[dict[str, Any]]:
    """Add ``ROUTE_FIELD`` to every row in place and return ``rows``."""
    routes = {str(route).casefold() for route in route_ids}
    served = _served_tier_models(gateway) if routes else {}
    for row in rows:
        identifier = str(row.get("id") or "")
        if identifier.casefold() in routes:
            row[ROUTE_FIELD] = _route_origin(identifier, serve_target, bridge_provider, served)
        else:
            row[ROUTE_FIELD] = {"kind": "model", "provider": OLLAMA}
    return rows


__all__ = ["ROUTE_FIELD", "annotate_model_rows"]
