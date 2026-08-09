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


def _bounded_limit(value: object, default: int = 50, maximum: int = 200) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
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
            if not normalized:
                return ToolResult(
                    ok=False,
                    output="preference text is empty.",
                    error_code="INVALID_INPUT",
                )
            selected_scope = scope or "global"
            rows = self._repository.upsert_and_list(
                scope=selected_scope,
                key=self._codec.key(normalized),
                text=normalized,
                confidence=0.8,
                limit=20,
            )
            output = "Learned preference: %s\n\n%s" % (
                normalized,
                self._codec.format(rows),
            )
        except Exception as exc:
            return ToolResult(
                ok=False, output=str(exc), error_code="PREFERENCE_STORAGE_ERROR"
            )
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
            output = "learned preferences\n%s" % self._codec.format(rows)
        except Exception as exc:
            return ToolResult(
                ok=False, output=str(exc), error_code="PREFERENCE_STORAGE_ERROR"
            )
        return ToolResult(ok=True, output=output)

    def disable(self, target: str, scope: str = "global") -> ToolResult:
        try:
            changed = self._repository.set_enabled(
                target, enabled=False, scope=scope or "global"
            )
        except Exception as exc:
            return ToolResult(
                ok=False, output=str(exc), error_code="PREFERENCE_STORAGE_ERROR"
            )
        self._changed("disabled", changed)
        return ToolResult(
            ok=True, output="forgot %d matching preference(s)" % changed
        )
