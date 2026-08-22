"""Focused contract tests for the packaged serve temperature policy."""

import pytest

from sonder_runtime.interfaces.http.serve_policy import serve_temperature


def test_serve_temperature_defaults_when_unconfigured(monkeypatch):
    monkeypatch.delenv("SONDER_SERVE_TEMPERATURE", raising=False)
    assert serve_temperature() == pytest.approx(0.2)


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
    assert serve_temperature() == pytest.approx(0.2)


def test_server_retains_compatibility_alias(monkeypatch):
    import server

    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0.75")
    assert server._serve_temperature() == serve_temperature()
