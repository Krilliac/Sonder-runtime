"""Contracts for the REPL's known-error next-step hints.

Each trigger literal is re-asserted against the module that emits it, so a
reworded error cannot leave a hint pointing at text that no longer exists.
"""
import pytest

from sonder_runtime.adapters.observability.error_hint_formatting import error_hint
from sonder_runtime.domain.cloud_access import cloud_disabled_message
from sonder_runtime.adapters.model_error_formatting import (
    format_runtime_model_call_error,
)


class _ModelError:
    def __init__(self, kind="transport", *, cloud=False, status=None,
                 detail="boom", attempts=1, retry_after_seconds=None):
        self.kind = kind
        self.cloud = cloud
        self.status = status
        self.detail = detail
        self.attempts = attempts
        self.retry_after_seconds = retry_after_seconds


def test_cloud_disabled_hint_offers_the_local_alternative():
    message = cloud_disabled_message()
    assert "hosted/cloud tiers are disabled" in message  # trigger still real
    hint = error_hint(message)
    assert "/model" in hint
    assert "cloud" in hint


def test_unavailable_model_pin_hint_points_at_model_listing():
    refusal = "model pin 'nomic-embed' is unavailable or is not chat-capable."
    hint = error_hint(refusal)
    assert "/model" in hint


def test_incompatible_model_pin_hint_names_the_tier_switch():
    refusal = "model pin 'llava' is incompatible with the selected code route."
    hint = error_hint(refusal)
    assert "/model <tier>" in hint


def test_local_ollama_contact_failure_suggests_starting_ollama():
    message = format_runtime_model_call_error(
        _ModelError(), endpoint_loopback=True, display="127.0.0.1:11434",
    )
    assert message.startswith("ERROR contacting local Ollama")
    hint = error_hint(message)
    assert "running" in hint


def test_remote_ollama_contact_failure_names_the_remote_opt_in():
    message = format_runtime_model_call_error(
        _ModelError(), endpoint_loopback=False, display="10.0.0.9:11434",
    )
    assert message.startswith("ERROR contacting remote Ollama")
    hint = error_hint(message)
    assert "SONDER_ALLOW_REMOTE_OLLAMA=1" in hint


def test_hosted_contact_failure_gets_a_network_hint():
    message = format_runtime_model_call_error(
        _ModelError(cloud=True), endpoint_loopback=True, display="api.example",
    )
    assert message.startswith("ERROR contacting hosted Ollama")
    assert error_hint(message) != ""


def test_http_404_hint_explains_missing_model_tags():
    message = format_runtime_model_call_error(
        _ModelError(kind="http", status=404),
        endpoint_loopback=True, display="127.0.0.1:11434",
    )
    assert "rejected the model request (HTTP 404" in message
    hint = error_hint(message)
    assert "ollama pull" in hint


@pytest.mark.parametrize("status", [408, 429, 502, 503, 504])
def test_transient_http_statuses_hint_at_retry(status):
    message = format_runtime_model_call_error(
        _ModelError(kind="http", status=status),
        endpoint_loopback=True, display="127.0.0.1:11434",
    )
    hint = error_hint(message)
    assert str(status) in hint
    assert "transient" in hint


def test_plan_mode_refusal_hint_points_at_mode_command():
    refusal = "refused /write: file mutation is planned only (mode: plan)"
    hint = error_hint(refusal)
    assert "/mode" in hint


def test_unknown_errors_get_no_hint_at_all():
    assert error_hint("ERROR: something novel exploded") == ""
    assert error_hint("a perfectly normal answer") == ""
    assert error_hint("") == ""
    assert error_hint(None) == ""


def test_model_authored_prose_mentioning_a_pin_is_not_hinted():
    # The pin trigger needs the exact refusal suffix, not just the phrase.
    assert error_hint("model pin 'x' is great") == ""
