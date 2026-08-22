from __future__ import annotations

import pytest

from sonder_runtime.adapters import web_fetch
from sonder_runtime.application.context import local_owner_context


def test_web_fetch_requires_explicit_cloud_consent(monkeypatch):
    with pytest.raises(PermissionError):
        web_fetch.fetch(
            "https://example.test", context=local_owner_context(correlation_id="web")
        )


def test_web_fetch_preserves_bounded_text_and_redacts_block_page(monkeypatch):
    monkeypatch.setattr(web_fetch, "_web_tools", lambda: type("Web", (), {
        "enabled": staticmethod(lambda: True),
        "_request": staticmethod(
            lambda url, timeout=10: (
                b"<html>Access Denied</html>", "text/html"
            )
        ),
    })())
    result = web_fetch.fetch(
        "https://example.test", max_chars=1200,
        context=local_owner_context(correlation_id="web", cloud_allowed=True),
    )
    assert result["ok"] is False
    assert "Access Denied" in result["text"]


def test_web_fetch_raw_preserves_transport_monkeypatch_seam(monkeypatch):
    calls = []
    monkeypatch.setattr(web_fetch, "_web_tools", lambda: type("Web", (), {
        "enabled": staticmethod(lambda: True),
        "_request": staticmethod(
            lambda url, timeout=10: calls.append((url, timeout))
            or (bytes((99, 97, 102, 233)), "text/plain; charset=iso-8859-1")
        ),
    })())

    assert web_fetch.fetch_raw("https://example.test", max_chars=1200, timeout=4) == "café"
    assert calls == [("https://example.test", 4)]
