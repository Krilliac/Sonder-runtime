"""Preference-management use cases independent of MCP and legacy modules."""
from __future__ import annotations

from ..ports.preferences import (
    PreferenceCodec,
    PreferenceEventSink,
    PreferenceRepository,
)
from ..ports.tool_executor import ToolResult


def render_preference_result(result: ToolResult) -> str:
    """Preserve historical MCP/REPL text while retaining typed failures."""
    return result.output if result.ok else "ERROR: %s" % result.output


def _failure(code: str, public_message: str) -> ToolResult:
    """Return a typed, stable error without disclosing adapter internals."""
    return ToolResult(ok=False, output=public_message, error_code=code)


def _codec_failure() -> ToolResult:
    return _failure("PREFERENCE_CODEC_ERROR", "preference processing failed.")


def _storage_failure() -> ToolResult:
    return _failure("PREFERENCE_STORAGE_ERROR", "preference storage is unavailable.")


def _bounded_limit(value: object, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(1, min(parsed, maximum))


class PreferenceService:
    def __init__(
        self,
        repository: PreferenceRepository,
        codec: PreferenceCodec,
        events: PreferenceEventSink,
    ) -> None:
        self._repository = repository
        self._codec = codec
        self._events = events

    def _changed(self, operation: str, count: int) -> None:
        try:
            self._events.changed(operation, count)
        except Exception:
            # Observability is metadata-only and cannot decide business success.
            pass

    def learn(self, text: str, scope: str = "global") -> ToolResult:
        try:
            extracted = self._codec.extract(text)
            normalized = extracted[0] if extracted else self._codec.normalize(text)
            key = self._codec.key(normalized) if normalized else ""
            stable = self._codec.is_stable(normalized, source_text=text)
        except Exception:
            return _codec_failure()
        if not normalized:
            return _failure("INVALID_INPUT", "preference text is empty.")
        if not stable:
            return _failure(
                "INVALID_INPUT",
                "preference must describe a stable behavior or default.",
            )
        selected_scope = scope or "global"
        try:
            rows = self._repository.upsert_and_list(
                scope=selected_scope,
                key=key,
                text=normalized,
                confidence=0.8,
                limit=20,
            )
        except Exception:
            return _storage_failure()
        try:
            output = "Learned preference: %s\n\n%s" % (
                normalized,
                self._codec.format(rows),
            )
        except Exception:
            return _codec_failure()
        self._changed("learned", 1)
        return ToolResult(ok=True, output=output)

    def status(
        self, include_disabled: bool = False, limit: int = 50
    ) -> ToolResult:
        try:
            rows = self._repository.list(
                limit=_bounded_limit(limit, 50, 200),
                include_disabled=bool(include_disabled),
            )
        except Exception:
            return _storage_failure()
        try:
            output = "learned preferences\n%s" % self._codec.format(rows)
        except Exception:
            return _codec_failure()
        return ToolResult(ok=True, output=output)

    def disable(self, target: str, scope: str = "global") -> ToolResult:
        try:
            changed = self._repository.set_enabled(
                target, enabled=False, scope=scope or "global"
            )
        except Exception:
            return _storage_failure()
        self._changed("disabled", changed)
        return ToolResult(
            ok=True, output="forgot %d matching preference(s)" % changed
        )
