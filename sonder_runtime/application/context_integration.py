"""Typed application path for bounded context assembly.

The planner owns model and section budgets; the selection policy owns which
immutable candidates fit each section.  This use case only composes those
two decisions and returns a new snapshot.  It does not render, compact, or
mutate producer-owned context.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from sonder_runtime.domain.context.hardware_sizing import (
    ContextSizing,
    MeasuredContextCapability,
    size_native_context,
)
from sonder_runtime.domain.context.priority import ContextItem, Selection

from .context_planner import CONTEXT_SECTIONS, ContextPlan, ContextPlanner, ModelContext
from .context_priority import select_context
from .context_manifests import (
    ContextRecord,
    LastGoodSnapshot,
    PrefixManifest,
    PrefixManifestCache,
    ReplayManifest,
    build_replay_manifest,
    deduplicate_context,
)
from .context_overflow_recovery import ContextOverflowRecovery, RecoveryResult


@dataclass(frozen=True)
class ContextAssembly:
    """An immutable, explainable result for one application context request."""

    plan: ContextPlan
    selections: Mapping[str, Selection]
    sizing: ContextSizing | None = None
    prefix: PrefixManifest | None = None
    replay: ReplayManifest | None = None

    def __post_init__(self) -> None:
        selections = dict(self.selections)
        if tuple(selections) != CONTEXT_SECTIONS:
            raise ValueError("selections must contain every context section in order")
        object.__setattr__(self, "selections", MappingProxyType(selections))

    def __deepcopy__(self, memo: dict[int, object]) -> "ContextAssembly":
        """Keep immutable mapping proxies compatible with snapshot isolation."""
        copied = ContextAssembly(
            self.plan, dict(self.selections), self.sizing, self.prefix, self.replay
        )
        memo[id(self)] = copied
        return copied

    @property
    def selected(self) -> tuple[ContextItem, ...]:
        """Return selected items in stable section and producer order."""
        return tuple(
            item
            for section in CONTEXT_SECTIONS
            for item in self.selections[section].selected
        )

    @property
    def selected_tokens(self) -> int:
        return sum(item.cost for item in self.selected)


class ContextAssemblyService:
    """Compose typed planning and selection for a production caller."""

    def __init__(self, planner: ContextPlanner | None = None) -> None:
        self._planner = planner or ContextPlanner()

    def assemble(
        self,
        model: ModelContext,
        items: Mapping[str, tuple[ContextItem, ...] | list[ContextItem]],
        section_budgets: Mapping[str, int],
    ) -> ContextAssembly:
        """Plan and select items without mutating the supplied candidate lists."""
        unknown = set(items).difference(CONTEXT_SECTIONS)
        if unknown:
            raise ValueError(f"items contains unknown section(s): {sorted(unknown)}")
        candidates = {
            section: tuple(items.get(section, ())) for section in CONTEXT_SECTIONS
        }
        requested = {
            section: sum(item.cost for item in candidates[section])
            for section in CONTEXT_SECTIONS
        }
        plan = self._planner.plan(model, requested, section_budgets)
        selections = {
            section: select_context(candidates[section], budget=plan.budget_for(section))
            for section in CONTEXT_SECTIONS
        }
        result = ContextAssembly(plan, selections)
        if result.selected_tokens > plan.input_budget_tokens:
            raise AssertionError("context assembly exceeded the model input budget")
        return result


class ContextPlanningFacade:
    """Provider-neutral production boundary for one context decision.

    [any thread, pure apart from bounded in-memory caches] The facade owns
    composition of model/section budgets, deterministic selection, measured
    hardware limits, explainability, prefix/replay manifests, and last-good
    publication.  It never discovers hardware, renders provider payloads, or
    calls a model gateway; adapters supply measurements and candidates.
    """

    def __init__(
        self,
        *,
        planner: ContextPlanner | None = None,
        assembly_service: ContextAssemblyService | None = None,
        prefix_cache: PrefixManifestCache | None = None,
        recovery: ContextOverflowRecovery[Any] | None = None,
    ) -> None:
        self._assembly = assembly_service or ContextAssemblyService(planner)
        self._prefix_cache = prefix_cache or PrefixManifestCache()
        self._last_good = LastGoodSnapshot()
        self._recovery = recovery

    def assemble(
        self,
        model: ModelContext,
        items: Mapping[str, tuple[ContextItem, ...] | list[ContextItem]],
        section_budgets: Mapping[str, int],
        *,
        capability: MeasuredContextCapability | None = None,
        records: Sequence[ContextRecord] = (),
        prefix_version: str = "1",
        request_id: str | None = None,
        replay_metadata: Mapping[str, Any] | None = None,
    ) -> ContextAssembly:
        """Assemble a bounded, explainable view and publish only complete views.

        A supplied capability can only reduce the requested model window. No
        hardware fact is inferred when it is absent or invalid.
        """
        sizing = size_native_context(capability) if capability is not None else None
        effective_model = model
        if sizing is not None:
            effective_model = replace(
                model,
                context_window_tokens=min(model.context_window_tokens, sizing.context_tokens),
            )
            if effective_model.reserved_output_tokens >= effective_model.context_window_tokens:
                raise ValueError("hardware-aware context limit leaves no input budget")

        manifest_records = tuple(records)
        deduped = deduplicate_context(manifest_records).retained if manifest_records else ()
        prefix = self._prefix_cache.resolve(deduped, version=prefix_version) if manifest_records else None
        replay = (
            build_replay_manifest(
                request_id,
                effective_model.model,
                deduped,
                prefix_key=prefix.cache_key if prefix is not None else "",
                metadata=replay_metadata,
            )
            if request_id is not None
            else None
        )
        result = self._assembly.assemble(effective_model, items, section_budgets)
        result = ContextAssembly(result.plan, result.selections, sizing, prefix, replay)
        if not any(selection.emergency_overflow for selection in result.selections.values()):
            self._last_good.publish(result)
        return result

    def last_good(self) -> ContextAssembly | None:
        """Return an isolated complete assembly, if one has been published."""
        snapshot = self._last_good.get()
        return None if snapshot is None else snapshot.value

    def prepare_overflow(
        self,
        candidate: Any,
        *,
        estimated_tokens: int,
        context_limit: int,
        reserved_output_tokens: int = 0,
        compact: Callable[[Any], Any | None],
    ) -> RecoveryResult[Any]:
        """Expose the bounded preflight recovery seam without provider logic."""
        policy = self._recovery or ContextOverflowRecovery(
            compact=compact, shrink=lambda value, _factor: None
        )
        return policy.prepare(
            candidate,
            estimated_tokens=estimated_tokens,
            context_limit=context_limit,
            reserved_output_tokens=reserved_output_tokens,
        )

    def recover_overflow(
        self,
        candidate: Any,
        *,
        fits: Callable[[Any], bool],
        compact: Callable[[Any], Any | None],
        shrink: Callable[[Any, float], Any | None],
        max_attempts: int = 3,
    ) -> RecoveryResult[Any]:
        """Run bounded overflow recovery and preserve its last-good fallback."""
        policy = self._recovery or ContextOverflowRecovery(
            compact=compact, shrink=shrink, max_attempts=max_attempts
        )
        result = policy.recover(candidate, overflow=True, fits=fits)
        return result


# Both names describe the same stable application boundary; the shorter alias
# keeps callers from coupling to the internal planning terminology.
ContextAssemblyFacade = ContextPlanningFacade


__all__ = [
    "ContextAssembly", "ContextAssemblyService", "ContextPlanningFacade",
    "ContextAssemblyFacade",
]
