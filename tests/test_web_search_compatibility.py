from __future__ import annotations

import ast
from pathlib import Path

import web_tools
from sonder_runtime.adapters import web_search


def test_root_search_surface_delegates_to_packaged_owner(monkeypatch):
    rows = [{"title": "Example", "url": "https://example.test", "snippet": ""}]
    calls = []

    def fake_search_raw(query, *, limit, timeout):
        calls.append((query, limit, timeout))
        return rows

    monkeypatch.setattr(web_search, "search_raw", fake_search_raw)
    assert web_tools.web_search("example", limit=3, timeout=4) == rows
    assert calls == [("example", 3, 4)]


def test_root_formatter_delegates_to_packaged_owner(monkeypatch):
    monkeypatch.setattr(web_search, "format_results", lambda rows: "packaged")
    assert web_tools.format_search_results([]) == "packaged"


def test_root_web_tools_no_longer_defines_search_algorithm():
    source = Path("web_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="web_tools.py")
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "web_search" not in names
    assert "search_raw" in {
        node.name
        for node in ast.parse(
            Path("sonder_runtime/adapters/web_search.py").read_text(encoding="utf-8")
        ).body
        if isinstance(node, ast.FunctionDef)
    }
