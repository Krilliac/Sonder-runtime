from __future__ import annotations

from pathlib import Path

import web_tools
from sonder_runtime.adapters import web_fetch


def test_root_web_fetch_delegates_to_packaged_owner(monkeypatch):
    calls = []

    def fake_fetch_raw(url, *, max_chars=8000, timeout=10):
        calls.append((url, max_chars, timeout))
        return "packaged text"

    monkeypatch.setattr(web_fetch, "fetch_raw", fake_fetch_raw)
    assert web_tools.web_fetch("https://example.test", max_chars=123, timeout=6) == "packaged text"
    assert calls == [("https://example.test", 123, 6)]


def test_root_contains_only_compatibility_fetch_delegate():
    source = Path("web_tools.py").read_text(encoding="utf-8")
    assert "def web_fetch(" in source
    assert "def _decode_web_document(" not in source
    assert "from sonder_runtime.adapters.web_fetch import fetch_raw" in source


def test_packaged_fetch_exposes_canonical_raw_entrypoint():
    assert callable(web_fetch.fetch_raw)
