"""Provider-error rendering policy for model transport failures."""
from __future__ import annotations


TRANSIENT_MODEL_HTTP_CODES = frozenset({408, 429, 502, 503, 504})


def format_model_call_error(error, *, target: str, display: str) -> str:
    """Render a bounded model error without owning endpoint classification."""
    suffix = " after %d attempt(s)" % error.attempts
    if error.kind == "budget":
        return "ERROR: hosted agent output budget exhausted: %s" % error.detail
    if error.kind == "http":
        retry_hint = ""
        if error.cloud and error.status in TRANSIENT_MODEL_HTTP_CODES:
            retry_hint = (
                " Cloud calls are not retried automatically to avoid duplicate "
                "metered work."
            )
            if error.retry_after_seconds is not None:
                retry_hint += " Provider suggests retrying after about %ss." % (
                    int(round(error.retry_after_seconds)),
                )
        return "ERROR: %s rejected the model request (HTTP %s)%s: %s%s" % (
            target, error.status or "unknown", suffix, error.detail, retry_hint,
        )
    if error.kind == "configuration":
        return "ERROR: %s" % error.detail
    if error.kind in ("protocol", "empty_response", "request"):
        return "ERROR: invalid response from %s%s: %s" % (
            target, suffix, error.detail,
        )
    if error.kind == "cancelled":
        return "ERROR: %s" % error.detail
    return "ERROR contacting %s at %s%s: %s" % (
        target, display, suffix, error.detail,
    )


def format_runtime_model_call_error(
    error, *, endpoint_loopback: bool, display: str,
) -> str:
    """Render a runtime model error after classifying its endpoint target."""
    target = (
        "hosted Ollama" if error.cloud else
        "remote Ollama" if not endpoint_loopback else
        "local Ollama"
    )
    return format_model_call_error(error, target=target, display=display)


__all__ = [
    "TRANSIENT_MODEL_HTTP_CODES",
    "format_model_call_error",
    "format_runtime_model_call_error",
]
