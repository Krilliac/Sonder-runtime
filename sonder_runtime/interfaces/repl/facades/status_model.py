"""Bounded, root-free REPL status and model-selection presentation.

The facade consumes snapshots and callables supplied by the composition root;
it never imports the legacy ``server`` module or performs provider I/O.  This
keeps terminal presentation testable while the old command handlers continue
to own unrelated command behavior during migration.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


@dataclass(frozen=True)
class InstalledModel:
    """A bounded display record from a provider model catalog."""

    name: str
    size: str = ""


class ModelSelectionFacade:
    """Normalize model/tier snapshots for the REPL selection route."""

    @staticmethod
    def resolved_model(
        tiers: Mapping[str, Any] | None,
        tier: str,
        override: str | None = None,
    ) -> str:
        """Resolve the display model without owning mutable runtime state."""
        values = tiers if isinstance(tiers, Mapping) else {}
        return str(override or values.get(tier) or "unknown")

    @staticmethod
    def installed_models(payload: Any) -> tuple[InstalledModel, ...] | None:
        """Parse a provider catalog without assuming a provider or transport.

        ``None`` means the catalog was unavailable or malformed; an empty
        tuple is a valid reachable catalog with no installed models.
        """
        models = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(models, list):
            return None
        result: list[InstalledModel] = []
        for item in models:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                continue
            size_value = item.get("size")
            try:
                size = "%.1f GB" % (float(size_value) / (1024 ** 3)) if size_value else ""
            except (TypeError, ValueError, OverflowError):
                size = ""
            result.append(InstalledModel(name, size))
        return tuple(sorted(result, key=lambda value: value.name))

    @staticmethod
    def selectable_tiers(
        available: Mapping[str, Any] | None,
        configured: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Return the policy-filtered tiers, with a safe configured fallback."""
        if isinstance(available, Mapping):
            return dict(available)
        return dict(configured) if isinstance(configured, Mapping) else {}

    @staticmethod
    def withheld_reason(
        name: str,
        *,
        configured: Mapping[str, Any] | None,
        available: Mapping[str, Any] | None,
        cloud_tiers: Iterable[str] = (),
        cloud_disabled_message: str = "",
    ) -> str:
        """Explain a configured tier omitted by current availability policy."""
        configured = configured if isinstance(configured, Mapping) else {}
        available = available if isinstance(available, Mapping) else {}
        if not name or name not in configured or name in available:
            return ""
        if name in set(str(value) for value in cloud_tiers):
            return str(cloud_disabled_message).removeprefix("ERROR: ")
        return "tier %r is configured but not currently available" % name

    @staticmethod
    def choices(
        tiers: Mapping[str, Any],
        installed: Iterable[InstalledModel] | None,
    ) -> list[str]:
        """Build deterministic completion choices, giving tiers precedence."""
        tier_names = {str(name) for name in tiers if str(name)}
        model_names = {item.name for item in (installed or ()) if item.name}
        return sorted(tier_names, key=str.casefold) + sorted(
            model_names.difference(tier_names), key=str.casefold,
        )


class ExecutionStatusFacade:
    """Normalize an injected execution-status snapshot for prompt display."""

    def __init__(self, snapshot_provider: Callable[[], Any] | None = None) -> None:
        self._snapshot_provider = snapshot_provider

    def snapshot(self, status: Any = None) -> Mapping[str, Any] | None:
        if status is None and self._snapshot_provider is not None:
            try:
                status = self._snapshot_provider()
            except Exception:
                return None
        return status if isinstance(status, Mapping) and status.get("known") else None

    def counts(self, status: Any = None) -> tuple[int | str, int | str]:
        value = self.snapshot(status)
        if value is None:
            return "?", "?"
        try:
            return int(value.get("running_lanes") or 0), int(value.get("running_agents") or 0)
        except (TypeError, ValueError, OverflowError):
            return "?", "?"

    def prompt_text(self, status: Any = None) -> str:
        value = self.snapshot(status)
        if value is None:
            return "[lanes ? | agents ?]"
        try:
            lanes = int(value.get("running_lanes") or 0)
            running = int(value.get("running_agents") or 0)
            queued = int(value.get("queued_agents") or 0)
        except (TypeError, ValueError, OverflowError):
            return "[lanes ? | agents ?]"
        agents = str(running) if queued == 0 else "%s+%sq" % (running, queued)
        return "[lanes %s | agents %s]" % (lanes, agents)


class PermissionModeFacade:
    """Normalize permission mode/elevation for terminal presentation.

    The REPL only renders a supplied snapshot; the composition owner decides
    how the snapshot is obtained.  Missing or malformed state is represented
    as ``None`` so a cosmetic header cannot invent a safety posture.
    """

    _SHORT_LABELS = {
        "plan": "plan",
        "manual": "manual",
        "acceptEdits": "edits",
        "auto": "auto",
    }

    def __init__(self, snapshot_provider: Callable[[], Any] | None = None) -> None:
        self._snapshot_provider = snapshot_provider

    def snapshot(self, value: Any = None) -> Mapping[str, Any] | None:
        if value is None and self._snapshot_provider is not None:
            try:
                value = self._snapshot_provider()
            except Exception:
                return None
        if not isinstance(value, Mapping):
            return None
        mode = str(value.get("mode") or "").strip()
        if not mode:
            return None
        return value

    def label(self, value: Any = None) -> str:
        state = self.snapshot(value)
        if state is None:
            return ""
        return str(state.get("label") or state.get("mode") or "unknown")

    def short_label(self, value: Any = None) -> str:
        state = self.snapshot(value)
        if state is None:
            return "?"
        mode = str(state.get("mode") or "")
        return self._SHORT_LABELS.get(mode, mode[:8] or "?")

    def elevated(self, value: Any = None) -> bool:
        state = self.snapshot(value)
        return bool(state.get("elevated")) if state is not None else False

    def elevation_reason(self, value: Any = None) -> str:
        state = self.snapshot(value)
        return str(state.get("elevationReason") or "").strip() if state else ""


class ContextHealthFacade:
    """Read and normalize a bounded context-health snapshot."""

    def __init__(self, snapshot_provider: Callable[..., Any] | None = None) -> None:
        self._snapshot_provider = snapshot_provider

    def snapshot(self, session_id: str, project: str) -> dict[str, int] | None:
        if self._snapshot_provider is None:
            return None
        try:
            data = self._snapshot_provider(session=session_id, project=project)
            used = max(0, int(data.get("estimated_tokens") or 0))
            limit = max(1, int(data.get("context_limit") or 1))
        except Exception:
            return None
        return {"used": used, "limit": limit, "left": max(0, limit - used)}


__all__ = [
    "ContextHealthFacade",
    "ExecutionStatusFacade",
    "InstalledModel",
    "ModelSelectionFacade",
    "PermissionModeFacade",
]
