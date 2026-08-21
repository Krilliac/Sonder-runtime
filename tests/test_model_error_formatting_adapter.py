from __future__ import annotations

from types import SimpleNamespace

from sonder_runtime.adapters.model_error_formatting import format_model_call_error


def test_model_error_formatter_preserves_http_retry_hint():
    error = SimpleNamespace(
        kind="http", cloud=True, status=503, attempts=2,
        retry_after_seconds=2.4, detail="busy",
    )
    rendered = format_model_call_error(error, target="hosted Ollama", display="host")
    assert rendered.startswith("ERROR: hosted Ollama rejected the model request")
    assert "about 2s" in rendered


def test_model_error_formatter_handles_unknown_failure():
    error = SimpleNamespace(
        kind="other", cloud=False, attempts=1, detail="failed",
    )
    assert format_model_call_error(error, target="local Ollama", display="local") == (
        "ERROR contacting local Ollama at local after 1 attempt(s): failed"
    )
