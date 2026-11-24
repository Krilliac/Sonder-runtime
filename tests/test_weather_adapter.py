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


def test_weather_place_resolves_commaless_city_state_and_country(monkeypatch):
    seen_queries = []

    def fake_json_request(url, timeout=10):
        import urllib.parse
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("name", [""])[0]
        seen_queries.append(name)
        if name.lower() == "wichita":
            return {
                "results": [
                    {"name": "Wichita Falls", "admin1": "Texas", "country": "United States"},
                    {"name": "Wichita", "admin1": "Kansas", "country": "United States"},
                ]
            }
        if name.lower() == "charlotte":
            return {
                "results": [
                    {"name": "Charlotte", "admin1": "Michigan", "country": "United States"},
                    {"name": "Charlotte", "admin1": "North Carolina", "country": "United States"},
                ]
            }
        return {"results": []}

    monkeypatch.setattr(weather, "_json_request", fake_json_request)

    ks = weather._weather_place("wichita kansas", timeout=5)
    assert ks["name"] == "Wichita"
    assert ks["admin1"] == "Kansas"

    ks_abbr = weather._weather_place("wichita ks", timeout=5)
    assert ks_abbr["name"] == "Wichita"
    assert ks_abbr["admin1"] == "Kansas"

    nc = weather._weather_place("charlotte north carolina", timeout=5)
    assert nc["name"] == "Charlotte"
    assert nc["admin1"] == "North Carolina"
