from __future__ import annotations

import pytest

from sonder_runtime.adapters import weather
from sonder_runtime.application.context import local_owner_context


def test_weather_requires_explicit_consent():
    with pytest.raises(PermissionError):
        weather.lookup("Chicago", context=local_owner_context(correlation_id="weather"))


def test_weather_bounds_request_and_formats_provider_result(monkeypatch):
    calls = []

    def fake_lookup(location, forecast_days, units):
        calls.append((location, forecast_days, units))
        return {"place": {"name": "Chicago"}, "forecast": {}, "query": location}

    monkeypatch.setattr(weather, "weather_lookup", fake_lookup)
    monkeypatch.setattr(weather, "format_weather", lambda result: "Weather for Chicago")
    result = weather.lookup(
        "Chicago", forecast_days=100, units="metric",
        context=local_owner_context(correlation_id="weather", cloud_allowed=True),
    )
    assert result["ok"]
    assert calls == [("Chicago", 7, "metric")]
    assert weather.format_result(result) == "Weather for Chicago"
