"""Pure idempotency-key and durable replay validation for the launcher."""
from __future__ import annotations

import re


_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_REPLAY_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_REPLAY_HEADER_VALUE = re.compile(r"^[\x20-\x7e]{0,1024}$")


def normalize_idempotency_key(value):
    """Normalize and validate an optional launcher ``Idempotency-Key``."""
    key = str(value or "").strip()
    if key and not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError(
            "Idempotency-Key must be 8-128 letters, numbers, or . _ : -"
        )
    return key


def valid_command_replay(value):
    """Validate a durable response before it reaches ``BaseHTTPRequestHandler``."""
    if not isinstance(value, dict) or not set(value) <= {
        "status", "payload", "headers"
    }:
        return False
    status = value.get("status")
    if (
        isinstance(status, bool)
        or not isinstance(status, int)
        or not 100 <= status <= 599
    ):
        return False
    if not isinstance(value.get("payload"), dict):
        return False
    headers = value.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 16:
        return False
    return all(
        isinstance(name, str)
        and isinstance(header_value, str)
        and _REPLAY_HEADER_NAME.fullmatch(name)
        and _REPLAY_HEADER_VALUE.fullmatch(header_value)
        for name, header_value in headers.items()
    )


__all__ = ["normalize_idempotency_key", "valid_command_replay"]
