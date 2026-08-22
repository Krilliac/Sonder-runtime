"""Canonical bounded Open-Meteo weather adapter.

The root ``web_tools`` module remains the compatibility owner for the pinned
HTTP transport and JSON request seam.  Provider policy, geocoding, bounds,
and presentation live here so callers have one packaged implementation.
"""
from __future__ import annotations

import importlib
import re
import urllib.parse

from ..application.context import OperationContext


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_DOCS_URL = "https://open-meteo.com/en/docs"

_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Light freezing drizzle",
    57: "Heavy freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light rain showers", 81: "Rain showers", 82: "Heavy rain showers",
    85: "Light snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with light hail", 99: "Thunderstorm with heavy hail",
}
_US_STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut",
    "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa",
    "KS": "kansas", "KY": "kentucky", "LA": "louisiana", "ME": "maine",
    "MD": "maryland", "MA": "massachusetts", "MI": "michigan",
    "MN": "minnesota", "MS": "mississippi", "MO": "missouri",
    "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota",
    "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}


def _web_tools():
    return importlib.import_module("web_tools")


def _json_request(url, timeout=10):
    return _web_tools()._json_request(url, timeout=timeout)


def _weather_condition(code):
    try:
        value = int(code)
    except (TypeError, ValueError):
        return "Unknown conditions"
    return _WEATHER_CODES.get(value, "Weather code %d" % value)


def _wind_direction(degrees):
    try:
        value = float(degrees) % 360
    except (TypeError, ValueError):
        return ""
    points = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")
    return points[int((value + 11.25) // 22.5) % len(points)]


def _weather_place(location, timeout):
    queries = [location]
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if len(parts) > 1 and parts[0].lower() != location.lower():
        queries.append(parts[0])
    qualifiers = [_US_STATE_NAMES.get(part.upper(), part.lower()) for part in parts[1:]]
    for query in queries:
        geocode_url = "%s?%s" % (OPEN_METEO_GEOCODING_URL, urllib.parse.urlencode({
            "name": query, "count": 10, "language": "en", "format": "json",
        }))
        geocode = _json_request(geocode_url, timeout=timeout)
        matches = [match for match in (geocode.get("results") or []) if isinstance(match, dict)]
        if not matches:
            continue

        def score(place):
            searchable = " ".join(str(place.get(key) or "").lower() for key in (
                "name", "admin1", "admin2", "admin3", "country", "country_code",
            ))
            return sum(1 for qualifier in qualifiers if qualifier in searchable)

        return max(enumerate(matches), key=lambda row: (score(row[1]), -row[0]))[1]
    raise ValueError("no weather location matched %r" % location)


def weather_lookup(location, forecast_days=3, units="auto", timeout=10):
    if not _web_tools().enabled():
        raise RuntimeError("web tools disabled by SONDER_WEB_TOOLS")
    location = re.sub(r"\s+", " ", str(location or "")).strip()
    if len(location) < 2:
        raise ValueError("location must be a city/region or postal code")
    if len(location) > 120 or any(ord(char) < 32 for char in location):
        raise ValueError("location is too long or contains control characters")
    units = str(units or "auto").strip().lower()
    if units not in {"auto", "metric", "imperial"}:
        raise ValueError("units must be auto, metric, or imperial")
    forecast_days = max(1, min(int(forecast_days or 3), 7))
    place = _weather_place(location, timeout)
    try:
        latitude, longitude = float(place["latitude"]), float(place["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("weather geocoder omitted valid coordinates") from exc
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("weather geocoder returned invalid coordinates")
    resolved_units = units if units != "auto" else ("imperial" if place.get("country_code") == "US" else "metric")
    params = {
        "latitude": latitude, "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max",
        "timezone": "auto", "forecast_days": forecast_days,
    }
    if resolved_units == "imperial":
        params.update({"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"})
    forecast_url = "%s?%s" % (OPEN_METEO_FORECAST_URL, urllib.parse.urlencode(params))
    return {"query": location, "place": place, "units": resolved_units,
            "forecast": _json_request(forecast_url, timeout=timeout),
            "forecast_url": forecast_url, "source_url": OPEN_METEO_DOCS_URL}


def format_weather(result):
    place, forecast = result.get("place") or {}, result.get("forecast") or {}
    current, current_units = forecast.get("current") or {}, forecast.get("current_units") or {}
    daily, daily_units = forecast.get("daily") or {}, forecast.get("daily_units") or {}
    parts = [place.get("name"), place.get("admin1"), place.get("country")]
    display = ", ".join(str(part) for i, part in enumerate(parts) if part and part not in parts[:i]) or result.get("query") or "requested location"
    temp_unit, wind_unit, precip_unit = current_units.get("temperature_2m", ""), current_units.get("wind_speed_10m", ""), current_units.get("precipitation", "")
    direction = _wind_direction(current.get("wind_direction_10m"))
    wind = "%s %s" % (current.get("wind_speed_10m", "?"), wind_unit)
    if direction:
        wind += " " + direction
    lines = ["Weather for %s" % display,
             "Updated: %s (%s)" % (current.get("time", "unknown"), forecast.get("timezone", "local time")),
             "Now: %s, %s%s (feels like %s%s); humidity %s%%; wind %s; precipitation %s %s." % (_weather_condition(current.get("weather_code")), current.get("temperature_2m", "?"), temp_unit, current.get("apparent_temperature", "?"), temp_unit, current.get("relative_humidity_2m", "?"), wind, current.get("precipitation", "?"), precip_unit), "", "Forecast:"]
    for index, date in enumerate(daily.get("time") or []):
        def value(key, fallback="?"):
            values = daily.get(key) or []
            return values[index] if index < len(values) else fallback
        lines.append("- %s: %s; high %s%s, low %s%s; precipitation %s%% (%s %s); wind up to %s %s." % (date, _weather_condition(value("weather_code", None)), value("temperature_2m_max"), daily_units.get("temperature_2m_max", ""), value("temperature_2m_min"), daily_units.get("temperature_2m_min", ""), value("precipitation_probability_max"), value("precipitation_sum"), daily_units.get("precipitation_sum", ""), value("wind_speed_10m_max"), daily_units.get("wind_speed_10m_max", "")))
    lines.extend(["", "Source: Open-Meteo (%s)" % result.get("source_url", OPEN_METEO_DOCS_URL), "Live data: %s" % result.get("forecast_url", "")])
    return "\n".join(lines)


def lookup(location: str, *, forecast_days=3, units="auto", context: OperationContext):
    if not context.cloud_allowed:
        raise PermissionError("weather lookup requires explicit cloud consent")
    result = weather_lookup(
        location,
        forecast_days=max(1, min(int(forecast_days or 3), 7)),
        units=units,
    )
    return {"ok": True, "location": str(location), "result": result}


def format_result(result):
    return format_weather(result.get("result", {}))


__all__ = ["format_result", "format_weather", "lookup", "weather_lookup"]
