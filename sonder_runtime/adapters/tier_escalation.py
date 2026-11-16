"""Supply model-list data to the pure escalation planner at operation time."""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import sonder_runtime.application.routing.tier_escalation as _rules
from sonder_runtime.application.routing.tier_escalation import (  # noqa: F401 - public compatibility surface
    KNOB,
    MAX_ESCALATIONS,
    NON_ESCALATING_KINDS,
    Plan,
    REASON_EMPTY,
    REASON_FAILED,
    Rung,
    Step,
    VERIFIER_DETAIL,
    describe,
    enabled,
    failure_reason,
    single,
    verifier_reason,
)
from sonder_runtime.adapters.model_list_consumers import values


def plan(
    prompt: str, *, start: Rung, available: Iterable[str],
    resolve: Callable[[str], Rung | None], has_image: bool = False,
    max_escalations: int = MAX_ESCALATIONS, lists: Mapping[str, Any] | None = None,
) -> Plan:
    return _rules.plan(
        prompt, start=start, available=available, resolve=resolve, has_image=has_image,
        max_escalations=max_escalations, lists=values() if lists is None else lists,
    )


def reachable_tiers(
    start_tier: str, available: Iterable[str], *, has_image: bool = False,
    escalate: bool = True, lists: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    return _rules.reachable_tiers(
        start_tier, available, has_image=has_image, escalate=escalate,
        lists=values() if lists is None else lists,
    )
