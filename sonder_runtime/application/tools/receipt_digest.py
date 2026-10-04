"""Canonical result and argument digests shared by receipt publication and replay."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def receipt_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = ["receipt_digest"]
