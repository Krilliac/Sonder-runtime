"""Pure receipt safety for durable model-fanout rows.

Fanout answers are returned to the caller and also persisted, so a receipt
row must never retain recognizable credentials, and a claimed receipt must
match the run's immutable target snapshot and scope. Both checks are
explicit-input and side-effect free. Moved from ``server.py`` in the WP1
Three-Hundred-Thirteenth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json
import re

from sonder_runtime.domain.fanout_redaction import redact_prompt_echo


def safe_answer(value, prompt):
    """Return receipt-safe model output without retaining obvious credentials.

    Fanout answers are deliberately returned to the caller, but they are also
    durable receipt fields.  A model can repeat a credential from context or
    emit one while demonstrating a configuration snippet, so prompt-echo
    removal alone is not sufficient for that persistence boundary.  This is
    intentionally a narrow marker-based scrubber: ordinary prose remains
    useful, while recognizable bearer/header/key values are never stored.
    """
    rendered = redact_prompt_echo(value, prompt)
    rendered = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*"
        r"(?:(?:bearer|basic)\s+)?[^\s\"',;}\]]+",
        "Authorization: <redacted>",
        rendered,
    )
    rendered = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}",
        "Bearer <redacted>",
        rendered,
    )
    rendered = re.sub(
        r"(?i)(^|[\s,{])([\"']?(?:password|passwd|secret|token|api[-_]?key|credential)[\"']?)"
        r"\s*[:=]\s*(?!<(?:redacted|nested)>)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
        r"\1\2=<redacted>",
        rendered,
    )
    return rendered


def snapshot_allows(run, model, *, is_cloud_model_name):
    """Check a claimed receipt against the run's immutable target contract.

    ``is_cloud_model_name(model)`` classifies the target; it is injected so
    the root delegate keeps the routing classifier's monkeypatch seam.
    """
    try:
        snapshot = json.loads(run.get("models_json") or "[]")
    except (TypeError, ValueError):
        snapshot = []
    selected = {str(name).casefold() for name in snapshot if str(name).strip()}
    if str(model).casefold() not in selected:
        return False
    scope = str(run.get("scope") or "local").casefold()
    cloud = is_cloud_model_name(model)
    return scope in ("all", "available") or (scope == "cloud" and cloud) or (scope == "local" and not cloud)
