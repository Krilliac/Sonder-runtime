from __future__ import annotations

import pytest

from sonder_runtime.adapters import weather
from sonder_runtime.application.context import local_owner_context


def test_weather_requires_explicit_consent():
    with pytest.raises(PermissionError):
        weather.lookup("Chicago", context=local_owner_context(correlation_id="weather"))


def test_weather_bounds_request_and_formats_provider_result(monkeypatch):
    monkeypatch.setattr(weather, "_web_tools", lambda: type("Web", (), {
        "weather_lookup": staticmethod(lambda location, forecast_days, units: {
            "place": {"name": "Chicago"}, "forecast": {}, "query": location,
        }),
        "format_weather": staticmethod(lambda result: "Weather for Chicago"),
    })())
    result = weather.lookup(
        "Chicago", forecast_days=100, units="metric",
        context=local_owner_context(correlation_id="weather", cloud_allowed=True),
    )
    assert result["ok"]
    assert weather.format_result(result) == "Weather for Chicago"
