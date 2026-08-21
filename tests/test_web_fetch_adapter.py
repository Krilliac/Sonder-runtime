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
        "web_fetch": staticmethod(lambda url, max_chars: "<html>Access Denied</html>"),
    })())
    result = web_fetch.fetch(
        "https://example.test", max_chars=1200,
        context=local_owner_context(correlation_id="web", cloud_allowed=True),
    )
    assert result["ok"] is False
    assert "Access Denied" in result["text"]
