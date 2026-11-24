"""Which reasoning effort level a chat turn uses, and where it came from.

Mirrors :mod:`sonder_runtime.domain.sampling_temperature` for model thinking
effort. The canonical levels match :mod:`sonder_runtime.domain.chat_template_policy`:

    off | minimal | low | medium | high | xhigh

``None`` (``auto`` / ``default`` / ``preset``) leaves the effort unset so the
model or provider default applies.

Precedence order:
1. ``request`` -- explicit ``reasoning_effort`` on this request;
2. ``session`` -- per-session value set via ``/effort`` or the UI selector;
3. ``lane``    -- optional lane/env default (``SONDER_REASONING_EFFORT``);
4. ``preset``  -- unset (``None``).
"""
from __future__ import annotations

from dataclasses import dataclass

from .common.errors import InvalidInput

SOURCE_REQUEST = "request"
SOURCE_SESSION = "session"
SOURCE_LANE = "lane"
SOURCE_PRESET = "preset"

EFFORT_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")
_EFFORT_SET = frozenset(EFFORT_LEVELS)
_OFF_ALIASES = frozenset({"none", "disable", "disabled", "false", "0"})
_LEVEL_ALIASES = {
    "min": "minimal",
    "med": "medium",
    "max": "xhigh",
    "ultra": "xhigh",
}
AUTO_WORDS = frozenset({"auto", "default", "preset", "reset", "clear"})


def check_effort(value: object) -> str | None:
    """Return canonical effort string, ``None`` for unset, or raise ``InvalidInput``."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str):
        raise InvalidInput(
            "reasoning_effort must be one of %s, or auto" % ", ".join(EFFORT_LEVELS)
        )
    raw = value.strip().lower()
    if not raw or raw in AUTO_WORDS:
        return None
    if raw in _OFF_ALIASES:
        return "off"
    raw = _LEVEL_ALIASES.get(raw, raw)
    if raw in _EFFORT_SET:
        return raw
    raise InvalidInput(
        "reasoning_effort must be one of %s, or auto (got %r)"
        % (", ".join(EFFORT_LEVELS), value)
    )


def parse_effort_setting(text: object) -> str | None:
    """Parse an ``/effort`` argument: one of EFFORT_LEVELS, or ``auto`` (``None``)."""
    raw = str(text if text is not None else "").strip()
    if not raw:
        raise InvalidInput(
            "say an effort level (%s) or auto" % ", ".join(EFFORT_LEVELS)
        )
    return check_effort(raw)


def format_effort(value: str | None) -> str:
    return "auto" if not value else str(value).strip().lower()


@dataclass(frozen=True)
class ResolvedEffort:
    """The reasoning effort a turn sends (``None``: unset/auto) and its source."""

    value: str | None
    source: str

    def describe(self) -> str:
        return "effective reasoning_effort=%s (source: %s)" % (
            format_effort(self.value), self.source,
        )


def resolve_effort(
    *, request: object = None, session: object = None, lane: object = None,
) -> ResolvedEffort:
    """Pick the first set effort in order request > session > lane > preset."""
    for source, value in (
        (SOURCE_REQUEST, request),
        (SOURCE_SESSION, session),
        (SOURCE_LANE, lane),
    ):
        checked = check_effort(value)
        if checked is not None:
            return ResolvedEffort(checked, source)
    return ResolvedEffort(None, SOURCE_PRESET)


__all__ = [
    "AUTO_WORDS",
    "EFFORT_LEVELS",
    "ResolvedEffort",
    "SOURCE_LANE",
    "SOURCE_PRESET",
    "SOURCE_REQUEST",
    "SOURCE_SESSION",
    "check_effort",
    "format_effort",
    "parse_effort_setting",
    "resolve_effort",
]
