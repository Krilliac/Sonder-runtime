from __future__ import annotations

import pytest

from sonder_runtime.adapters import web_search
from sonder_runtime.application.context import local_owner_context


def test_web_search_requires_explicit_consent():
    with pytest.raises(PermissionError):
        web_search.search("example", context=local_owner_context(correlation_id="search"))


def test_web_search_bounds_results_and_formats(monkeypatch):
    monkeypatch.setattr(web_search, "_web_tools", lambda: type("Web", (), {
        "enabled": staticmethod(lambda: True),
    })())
    rows = [{"title": "Example", "url": "https://example.test", "snippet": ""}]
    monkeypatch.setattr(web_search, "search_raw", lambda query, limit: rows)
    monkeypatch.setattr(
        web_search, "format_results", lambda result: "Example\nhttps://example.test"
    )
    result = web_search.search(
        "example", limit=100,
        context=local_owner_context(correlation_id="search", cloud_allowed=True),
    )
    assert result["ok"]
    assert web_search.format_result(result).startswith("Example")
