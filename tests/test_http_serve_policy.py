"""Focused contract tests for the packaged serve temperature policy."""

import pytest

from sonder_runtime.interfaces.http.serve_policy import (
    serve_lane_temperature,
    serve_temperature,
    serve_temperature_override,
    serve_temperature_override_value,
)


def test_serve_temperature_is_auto_when_unconfigured(monkeypatch):
    # No blanket 0.2: unset means "send none", so the model preset applies.
    monkeypatch.delenv("SONDER_SERVE_TEMPERATURE", raising=False)
    assert serve_temperature() is None
    assert serve_lane_temperature() is None


@pytest.mark.parametrize("raw", ["auto", "AUTO", " default ", "preset"])
def test_serve_temperature_auto_words(monkeypatch, raw):
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", raw)
    assert serve_temperature() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", 0.0), ("2", 2.0), ("9", 2.0), ("-1", 0.0)],
)
def test_serve_temperature_clamps_finite_values(monkeypatch, raw, expected):
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", raw)
    assert serve_temperature() == expected


@pytest.mark.parametrize("raw", ["warm", "nan", "inf", "-inf"])
def test_serve_temperature_ignores_invalid_or_nonfinite_values(monkeypatch, raw):
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", raw)
    assert serve_temperature() is None


def test_override_is_the_request_value_and_beats_the_env(monkeypatch):
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0.3")
    assert serve_temperature_override_value() is None
    with serve_temperature_override(1.1):
        assert serve_temperature_override_value() == pytest.approx(1.1)
        assert serve_temperature() == pytest.approx(1.1)
        assert serve_lane_temperature() == pytest.approx(0.3)
    assert serve_temperature_override_value() is None
    assert serve_temperature() == pytest.approx(0.3)


def test_server_retains_compatibility_alias(monkeypatch):
    import server

    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0.75")
    assert server._serve_temperature() == serve_temperature()
