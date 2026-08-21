"""Typed compatibility adapter for bounded public web text fetches."""
from __future__ import annotations

import importlib

from ..application.context import OperationContext


MAX_CHARS = 30_000


def _web_tools():
    return importlib.import_module("web_tools")


def _artifact_fetch():
    return importlib.import_module("sonder_runtime.adapters.artifact_fetch")


def fetch(url: str, *, max_chars=8000, context: OperationContext):
    """Fetch readable public text only after explicit cloud consent."""
    if not context.cloud_allowed:
        raise PermissionError("web fetch requires explicit cloud consent")
    if not _web_tools().enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    bounded_chars = max(1000, min(int(max_chars or 8000), MAX_CHARS))
    text = _web_tools().web_fetch(url, max_chars=bounded_chars)
    artifact_fetch = _artifact_fetch()
    blocked = artifact_fetch.detect_block_page(
        text, content_type="text/html", url=url,
    )
    if blocked is not None:
        return {
            "ok": False, "url": str(url), "chars": 0,
            "blocked": blocked, "text": artifact_fetch.format_block_notice(url, blocked),
        }
    return {"ok": True, "url": str(url), "chars": len(text), "text": text}


def format_result(result):
    return str(result.get("text", ""))


__all__ = ["fetch", "format_result"]
