"""Typed compatibility adapter for consent-gated public web search."""
from __future__ import annotations

import importlib

from ..application.context import OperationContext


def _web_tools():
    return importlib.import_module("web_tools")


def search(query: str, *, limit=5, context: OperationContext):
    if not context.cloud_allowed:
        raise PermissionError("web search requires explicit cloud consent")
    if not _web_tools().enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    rows = _web_tools().web_search(query, limit=max(1, min(int(limit or 5), 20)))
    return {"ok": True, "query": str(query), "results": rows}


def format_result(result):
    return _web_tools().format_search_results(result.get("results", []))


__all__ = ["format_result", "search"]
