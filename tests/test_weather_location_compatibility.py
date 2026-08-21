from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
import web_tools
from sonder_runtime.adapters import location, weather


ROOT = Path(__file__).resolve().parents[1]


def test_root_weather_and_location_surfaces_are_packaged_aliases():
    assert web_tools.weather_lookup is weather.weather_lookup
    assert web_tools.format_weather is weather.format_weather
    assert web_tools.normalize_location_hint is location.normalize_location_hint
    assert web_tools.approximate_location_lookup is location.approximate_location_lookup
    assert web_tools.location_label is location.location_label
    assert web_tools.format_approximate_location is location.format_approximate_location


def test_root_has_no_public_weather_or_location_implementation_definitions():
    tree = ast.parse((ROOT / "web_tools.py").read_text(encoding="utf-8"))
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert not names.intersection({
        "weather_lookup", "format_weather", "normalize_location_hint",
        "approximate_location_lookup", "location_label",
        "format_approximate_location",
    })


def test_weather_preserves_pinned_transport_and_url_validation(monkeypatch):
    requested = []

    def fake_request(url, timeout=10):
        requested.append((url, timeout))
        if "geocoding-api" in url:
            return b'{"results":[{"name":"Chicago","admin1":"Illinois","country":"United States","country_code":"US","latitude":41.85,"longitude":-87.65}]}', "application/json"
        return b'{"timezone":"America/Chicago","current":{},"daily":{}}', "application/json"

    monkeypatch.setattr(web_tools, "_request", fake_request)
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    result = weather.weather_lookup("Chicago, IL", forecast_days=99, timeout=4)
    assert result["units"] == "imperial"
    assert len(requested) == 2
    assert all(url.startswith((weather.OPEN_METEO_GEOCODING_URL, weather.OPEN_METEO_FORECAST_URL)) for url, _ in requested)
    assert all(timeout == 4 for _, timeout in requested)

    with pytest.raises(ValueError, match="control characters"):
        weather.weather_lookup("Chicago\x01IL")
    with pytest.raises(ValueError, match="units"):
        weather.weather_lookup("Chicago", units="kelvin")


def test_weather_rejects_geocoder_coordinates_outside_bounds(monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setattr(weather, "_json_request", lambda *_args, **_kwargs: {
        "results": [{"name": "Bad", "latitude": 91, "longitude": 0}],
    })
    with pytest.raises(ValueError, match="invalid coordinates"):
        weather.weather_lookup("Bad place")


def test_location_consent_and_privacy_boundaries(monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    payload = {"success": True, "ip": "203.0.113.7", "city": "Chicago",
               "region": "Illinois", "country": "United States",
               "country_code": "US", "timezone": {"id": "America/Chicago"}}
    monkeypatch.setattr(location, "_json_request", lambda *_args, **_kwargs: payload)
    value = location.approximate_location_lookup()
    assert "ip" not in value
    assert location.location_label(value) == "Chicago, Illinois, United States"
    assert "203.0.113.7" not in location.format_approximate_location(value)

    with pytest.raises(ValueError, match="did not return a place"):
        location.normalize_location_hint({"success": True, "latitude": 1})
    with pytest.raises(ValueError, match="invalid city"):
        location.normalize_location_hint({"city": {"raw": "bad"}})


def test_packaged_adapters_are_canonical_source_files():
    assert Path(inspect.getsourcefile(weather)).resolve() == (ROOT / "sonder_runtime/adapters/weather.py").resolve()
    assert Path(inspect.getsourcefile(location)).resolve() == (ROOT / "sonder_runtime/adapters/location.py").resolve()
