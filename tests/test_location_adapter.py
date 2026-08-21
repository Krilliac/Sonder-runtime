from __future__ import annotations

import pytest

from sonder_runtime.adapters import location
from sonder_runtime.application.context import local_owner_context


def test_location_requires_both_consents():
    with pytest.raises(PermissionError):
        location.lookup(consent=True, context=local_owner_context(correlation_id="location"))
    with pytest.raises(PermissionError):
        location.lookup(consent=False, context=local_owner_context(correlation_id="location", cloud_allowed=True))


def test_location_durable_result_contains_no_raw_ip(monkeypatch):
    monkeypatch.setattr(location, "_web_tools", lambda: type("Web", (), {
        "approximate_location_lookup": staticmethod(lambda: {"city": "Chicago", "country": "US", "ip": "203.0.113.7"}),
        "location_label": staticmethod(lambda value: "Chicago, US"),
        "format_approximate_location": staticmethod(lambda value: "Approximate location: Chicago, US\nRaw IP: not retained or displayed."),
    })())
    result = location.lookup(
        consent=True,
        context=local_owner_context(correlation_id="location", cloud_allowed=True),
    )
    assert result["label"] == "Chicago, US"
    assert "203.0.113.7" not in result["text"]
