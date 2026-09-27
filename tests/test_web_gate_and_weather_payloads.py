"""Web-family refusals and malformed provider payloads stay structured.

Regression coverage for two defects found by the live surface sweep:

* With ``SONDER_WEB_TOOLS`` off (the shipped default) the packaged tool
  executor re-raised the adapters' gate ``RuntimeError``; over ``mcp --native``
  every web tool call became JSON-RPC ``-32603 internal MCP handler error``
  with an ERROR traceback instead of a clean ``isError`` refusal.
* The weather adapter crashed with ``TypeError``/``AttributeError`` (or a
  NaN/infinity conversion error) on provider JSON of the wrong shape, which
  escaped the executor for ``AttributeError`` and otherwise surfaced as an
  unrelated Python error string.
"""
from __future__ import annotations

import pytest

import web_tools
from sonder_runtime.adapters import location, weather, web_fetch, web_search
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall
from sonder_runtime.application.ports.web import WebToolsDisabled


def _consenting():
    return local_owner_context(correlation_id="web_gate", cloud_allowed=True)


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("web_fetch", {"url": "https://example.com/"}),
        ("web_search", {"query": "sonder"}),
        ("weather_lookup", {"location": "Paris"}),
        ("approximate_location_lookup", {"consent": True}),
    ],
)
def test_disabled_web_tools_refuse_as_structured_result(monkeypatch, tool, arguments):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")

    result = ToolExecutorAdapter().execute(ToolCall(tool, arguments), _consenting())

    assert result.ok is False
    assert result.error_code == "WebToolsDisabled"
    assert "SONDER_WEB_TOOLS" in result.output


def test_consent_is_still_checked_before_the_web_gate(monkeypatch):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")

    result = ToolExecutorAdapter().execute(
        ToolCall("web_fetch", {"url": "https://example.com/"}),
        local_owner_context(correlation_id="web_gate"),
    )

    assert result.error_code == "PermissionError"


@pytest.mark.parametrize(
    "call",
    [
        lambda: web_fetch.fetch_raw("https://example.com/"),
        lambda: web_search.search_raw("sonder"),
        lambda: weather.weather_lookup("Paris"),
        lambda: location.approximate_location_lookup(),
    ],
)
def test_gate_error_is_typed_and_remains_a_runtime_error(monkeypatch, call):
    monkeypatch.setenv("SONDER_WEB_TOOLS", "0")

    with pytest.raises(WebToolsDisabled) as caught:
        call()

    assert isinstance(caught.value, RuntimeError)


_PLACE = {"results": [{"name": "Paris", "latitude": 48.85, "longitude": 2.35}]}


def _serve(monkeypatch, *payloads):
    queue = list(payloads)
    monkeypatch.setenv("SONDER_WEB_TOOLS", "1")
    monkeypatch.setattr(web_tools, "_json_request", lambda url, timeout=10: queue.pop(0))


@pytest.mark.parametrize("results", [5, True, {"name": "Paris"}])
def test_malformed_geocoder_results_are_a_clean_value_error(monkeypatch, results):
    _serve(monkeypatch, {"results": results})

    with pytest.raises(ValueError, match="malformed"):
        weather.weather_lookup("Paris")


@pytest.mark.parametrize(
    "forecast",
    [
        {"current": [1, 2]},
        {"current_units": "C"},
        {"daily": [1]},
        {"daily_units": [1]},
        {"daily": {"time": 7}},
        {"daily": {"time": ["2026-09-25"], "temperature_2m_max": 5}},
    ],
)
def test_malformed_forecast_sections_are_a_clean_value_error(monkeypatch, forecast):
    _serve(monkeypatch, _PLACE, forecast)

    with pytest.raises(ValueError, match="malformed"):
        weather.weather_lookup("Paris")


@pytest.mark.parametrize(
    "current",
    [
        {"weather_code": float("inf"), "wind_direction_10m": float("nan")},
        {"weather_code": "inf", "wind_direction_10m": "inf"},
        {"weather_code": float("nan"), "wind_direction_10m": float("-inf")},
    ],
)
def test_non_finite_provider_numbers_format_without_raising(monkeypatch, current):
    _serve(monkeypatch, _PLACE, {"current": current})

    text = weather.format_weather(weather.weather_lookup("Paris"))

    assert "Weather for Paris" in text
    assert "Unknown conditions" in text


def test_malformed_weather_payload_is_a_structured_executor_refusal(monkeypatch):
    _serve(monkeypatch, _PLACE, {"current": [1, 2]})

    result = ToolExecutorAdapter().execute(
        ToolCall("weather_lookup", {"location": "Paris"}), _consenting()
    )

    assert result.ok is False
    assert result.error_code == "ValueError"
    assert "malformed" in result.output


def test_well_formed_forecast_still_formats(monkeypatch):
    _serve(monkeypatch, _PLACE, {
        "timezone": "Europe/Paris",
        "current": {"time": "2026-09-25T06:00", "temperature_2m": 14.4,
                    "weather_code": 0, "wind_direction_10m": 90},
        "current_units": {"temperature_2m": "C"},
        "daily": {"time": ["2026-09-25"], "weather_code": [3],
                  "temperature_2m_max": [20], "temperature_2m_min": [11]},
        "daily_units": {"temperature_2m_max": "C"},
    })

    text = weather.format_weather(weather.weather_lookup("Paris"))

    assert "Now: Clear sky, 14.4C" in text
    assert "wind ? " in text and " E;" in text
    assert "- 2026-09-25: Overcast; high 20C" in text
