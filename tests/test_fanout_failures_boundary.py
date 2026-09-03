"""Fanout failure classification lives in the adapters layer; root names are aliases."""
import time

import pytest

import server
from sonder_runtime.adapters import fanout_failures
from sonder_runtime.adapters.model_transport import ModelCallError


def test_root_names_are_identity_preserving_aliases():
    assert server._fanout_failure_class is fanout_failures.failure_class
    assert server._fanout_safe_error is fanout_failures.safe_error
    assert server._fanout_no_eligible_models_error is fanout_failures.no_eligible_models_error


@pytest.mark.parametrize("error, expected", [
    (ModelCallError("configuration", "x"), "configuration"),
    (ModelCallError("request", "x"), "request_rejected"),
    (ModelCallError("timeout", "x"), "timeout"),
    (ModelCallError("budget", "x"), "budget_exhausted"),
    (ModelCallError("cancelled", "x"), "cancelled"),
    (ModelCallError("http", "x", status=429), "throttled"),
    (ModelCallError("http", "x", status=408), "timeout"),
    (ModelCallError("http", "x", status=404), "unavailable"),
    (ModelCallError("http", "x", status=503), "unavailable"),
    (ModelCallError("http", "x", status=422), "request_rejected"),
    (ModelCallError("http", "x"), "unknown"),
    (ModelCallError("mystery", "x"), "unknown"),
    (RuntimeError("provider says secret"), "unknown"),
])
def test_failure_class_is_closed_and_content_free(error, expected):
    assert fanout_failures.failure_class(error) == expected


def test_safe_error_keeps_only_class_and_status_and_never_the_body():
    prompt = "please summarize the quarterly ledger for the finance team"
    http = ModelCallError("http", "provider body echoing " + prompt, status=502)
    assert fanout_failures.safe_error(http, prompt) == "ERROR: fanout model failure (http HTTP 502)"
    assert fanout_failures.safe_error(ModelCallError("timeout", "x"), prompt) == (
        "ERROR: fanout model failure (timeout)"
    )
    assert fanout_failures.safe_error(ValueError(prompt), prompt) == "ERROR: model request failed (ValueError)"


def test_no_eligible_models_error_summarizes_skips_without_names():
    error = fanout_failures.no_eligible_models_error({"skipped": []}, "local")
    assert isinstance(error, ModelCallError)
    assert error.kind == "configuration"
    assert error.detail == "no eligible local models are currently discovered."
    plan = {"scope": "cloud", "skipped": [
        {"model": "secret-name", "reason": "health cooldown active", "retry_after_ts": time.time() + 30},
        {"model": "other", "reason": "health cooldown active", "retry_after_ts": "bad"},
        {"model": "third", "reason": "non-chat target"},
        {"model": "fourth"},
    ]}
    error = fanout_failures.no_eligible_models_error(plan, "local")
    assert "secret-name" not in error.detail
    assert error.detail.startswith("no eligible cloud models are currently available; skipped: ")
    assert "health cooldown active (2)" in error.detail
    assert "non-chat target (1)" in error.detail
    assert "not eligible (1)" in error.detail
    assert "earliest cooldown retry in about" in error.detail
    assert error.detail.endswith("s.")
