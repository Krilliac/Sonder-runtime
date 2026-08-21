"""Explicit-consent approximate location adapter."""
from __future__ import annotations

import importlib

from ..application.context import OperationContext


def _web_tools():
    return importlib.import_module("web_tools")


def lookup(*, consent=False, context: OperationContext):
    if not consent or not context.cloud_allowed:
        raise PermissionError("explicit location and cloud consent are required")
    web = _web_tools()
    location = web.approximate_location_lookup()
    return {
        "ok": True,
        "label": web.location_label(location),
        "text": web.format_approximate_location(location),
    }


def format_result(result):
    return str(result.get("text", ""))


__all__ = ["format_result", "lookup"]
