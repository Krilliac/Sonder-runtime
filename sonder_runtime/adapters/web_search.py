"""Typed compatibility adapter for consent-gated public web search."""
from __future__ import annotations

import importlib

from ..application.context import OperationContext


def _web_tools():
    return importlib.import_module("web_tools")


def search_raw(query: str, *, limit=5, timeout=10):
    """Run the bounded provider search owned by this adapter.

    The root ``web_tools`` module remains the compatibility owner for the
    shared pinned HTTP transport and parsers.  Keeping those lower-level
    helpers injected through the module preserves existing test seams while
    moving provider selection, fallback, and relevance policy here.
    """
    tools = _web_tools()
    if not tools.enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    query = (query or "").strip()
    if not query:
        raise ValueError("empty search query")
    limit = max(1, min(int(limit or 5), 10))
    provider_query_root = tools._provider_search_query(query)
    configured_endpoint = tools.os.environ.get("SONDER_SEARCH_URL", "").strip()
    endpoints = (
        [configured_endpoint]
        if configured_endpoint
        else [
            tools.DEFAULT_SEARCH_URL,
            tools.MOJEEK_SEARCH_URL,
            tools.BING_SEARCH_RSS_URL,
        ]
    )
    failures = []
    best_results = []
    best_relevance = -1
    for endpoint in endpoints:
        provider_queries = (
            [provider_query_root]
            if configured_endpoint
            else tools._search_query_variants(provider_query_root)
        )
        for provider_query in provider_queries:
            url = endpoint.format(query=tools.urllib.parse.quote_plus(provider_query))
            try:
                raw, ctype = tools._request(url, timeout=timeout)
                text = raw.decode("utf-8", "replace")
                lowered = text.lower()
                if "automated queries" in lowered or "403 - forbidden" in lowered:
                    failures.append(
                        "%s blocked" % tools.urllib.parse.urlparse(url).hostname
                    )
                    break
                if "json" in ctype:
                    data = tools.json.loads(text)
                    rows = data.get("results") if isinstance(data, dict) else data
                    results = [
                        {
                            "title": str(row.get("title", "")),
                            "url": str(row.get("url", "")),
                            "snippet": str(row.get("snippet", "")),
                        }
                        for row in (rows or [])[:limit]
                        if isinstance(row, dict)
                    ]
                elif "xml" in ctype or text.lstrip().startswith("<?xml"):
                    results = tools._search_rss_rows(raw, limit)
                else:
                    results = tools._search_rows(text, url, limit)
                if results:
                    relevance, required = tools._search_relevance(
                        provider_query_root, results
                    )
                    if relevance > best_relevance:
                        best_results = results
                        best_relevance = relevance
                    if relevance >= required:
                        return results
                if "challenge-form" in text or "anomaly-modal" in text:
                    failures.append(
                        "%s challenge" % tools.urllib.parse.urlparse(url).hostname
                    )
                    break
            except Exception as exc:
                failures.append(
                    "%s %s" % (
                        tools.urllib.parse.urlparse(url).hostname,
                        type(exc).__name__,
                    )
                )
                break
    _best, required = tools._search_relevance(query, best_results)
    if best_results and best_relevance >= required:
        return best_results
    if best_results:
        raise RuntimeError("search providers returned no sufficiently relevant results")
    if failures and len(failures) == len(endpoints):
        raise RuntimeError("search providers unavailable: %s" % ", ".join(failures))
    return []


def search(query: str, *, limit=5, context: OperationContext):
    if not context.cloud_allowed:
        raise PermissionError("web search requires explicit cloud consent")
    if not _web_tools().enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    rows = search_raw(query, limit=max(1, min(int(limit or 5), 20)))
    return {"ok": True, "query": str(query), "results": rows}


def format_result(result):
    return format_results(result.get("results", []))


def format_results(results):
    if not results:
        return "(no results)"
    lines = []
    for i, row in enumerate(results, start=1):
        lines.append("%d. %s" % (i, row.get("title") or "(untitled)"))
        lines.append("   %s" % row.get("url", ""))
        if row.get("snippet"):
            lines.append("   %s" % row["snippet"])
    return "\n".join(lines)


__all__ = ["format_result", "format_results", "search", "search_raw"]
