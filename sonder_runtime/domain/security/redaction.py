"""Canonical secret-shape redaction patterns. Pure: stdlib only, no I/O.

This is the single home for the pattern set that scrubs credential shapes
out of arbitrary text. The runtime grew at least nine independent redaction
implementations; every new caller should compose this module instead of
writing a tenth. ``platform.logging.Redactor`` (the logging/error path) and
``application.tools.facade.PatternOutputRedactor`` (the tool-output path)
both delegate here, so the answer to "is this shape scrubbed?" no longer
depends on which path a secret takes.

The functions are deliberately total over their documented inputs and carry
no environment access: collecting live secret *values* (e.g. from
``SECRET_ENV_VARS``) is a platform concern and stays in the platform layer.
"""
from __future__ import annotations

import re
from typing import Iterable

REDACTED = "[REDACTED]"
REDACTION_FAILED = "[REDACTION_FAILED]"
WORKSPACE_LABEL = "[WORKSPACE]"

# Ordering note: value replacement happens before pattern replacement in
# redact_text, so a known secret value is scrubbed even where no pattern
# would match its surroundings.
PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(\S+(?:\s+\S+)?)"),
    re.compile(r"(?i)\b((?:set-)?cookie\s*:\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b(bearer\s+)([a-z0-9._~+/=-]{8,})"),
    re.compile(
        r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|"
        r"passwd|credential)[\"']?\s*[:=]\s*)([\"']?[^\s\"',;}{]{4,}[\"']?)"
    ),
    re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@"),
    re.compile(
        r"(?i)([?&](?:access[-_]?token|api[-_]?key|auth[-_]?secret|"
        r"password|credential)=)([^&#\s]+)"
    ),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_text(
    text: str,
    *,
    secret_values: Iterable[str] = (),
    path_prefixes: Iterable[str] = (),
) -> str:
    """Scrub known secret values, credential shapes, and path prefixes.

    ``secret_values`` are replaced longest-first so an overlapping shorter
    value cannot split a longer one into recognizable halves. Pure function:
    any failure propagates to the caller, which owns the fail-closed
    (``REDACTION_FAILED``) decision.
    """
    for value in sorted({v for v in secret_values if v}, key=len, reverse=True):
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    for prefix in path_prefixes:
        if prefix and prefix in text:
            text = text.replace(prefix, WORKSPACE_LABEL)
    return text


# Structure-walk bounds. A redactor that recurses without limits turns a
# hostile deeply-nested output into a crash inside the safety layer itself.
MAX_WALK_DEPTH = 32
MAX_WALK_ITEMS = 10_000


def redact_structure(value, redact=None, *, _depth=0, _budget=None):
    """Redact every string inside a JSON-shaped structure, preserving shape.

    ``redact`` is a ``str -> str`` callable (defaults to :func:`redact_text`
    with patterns only). Dict keys, numbers, bools, and None pass through
    unchanged. Beyond :data:`MAX_WALK_DEPTH` or :data:`MAX_WALK_ITEMS` the
    remaining subtree is replaced with :data:`REDACTED` rather than returned
    unexamined: when the walker must stop checking, it must not pass content
    it has stopped checking.
    """
    if redact is None:
        redact = redact_text
    if _budget is None:
        _budget = [MAX_WALK_ITEMS]
    _budget[0] -= 1
    if _depth > MAX_WALK_DEPTH or _budget[0] < 0:
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: redact_structure(item, redact, _depth=_depth + 1, _budget=_budget)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        walked = [
            redact_structure(item, redact, _depth=_depth + 1, _budget=_budget)
            for item in value
        ]
        return tuple(walked) if isinstance(value, tuple) else walked
    return value
