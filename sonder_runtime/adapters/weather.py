"""Typed consent-gated weather provider adapter."""
from __future__ import annotations

import importlib

from ..application.context import OperationContext


def _web_tools():
    return importlib.import_module("web_tools")


def lookup(location: str, *, forecast_days=3, units="auto", context: OperationContext):
    if not context.cloud_allowed:
        raise PermissionError("weather lookup requires explicit cloud consent")
    web = _web_tools()
    result = web.weather_lookup(
        location, forecast_days=max(1, min(int(forecast_days or 3), 7)), units=units,
    )
    return {"ok": True, "location": str(location), "result": result}


def format_result(result):
    return _web_tools().format_weather(result.get("result", {}))


__all__ = ["format_result", "lookup"]
