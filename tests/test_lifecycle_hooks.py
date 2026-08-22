from __future__ import annotations

import pytest

from sonder_runtime.application.lifecycle_hooks import (
    HookRegistrationError,
    LifecycleHookRegistry,
)


def test_hooks_dispatch_by_priority_and_isolate_failures():
    calls = []
    registry = LifecycleHookRegistry()
    registry.register("late", "turn.completed", lambda event, payload: calls.append("late"), priority=0)

    def broken(event, payload):
        raise RuntimeError("observer failed")

    registry.register("broken", "turn.completed", broken, priority=10)
    registry.register("early", "turn.completed", lambda event, payload: calls.append("early"), priority=10)

    result = registry.dispatch("turn.completed", {"session": "s1"})

    assert result.invoked == ("broken", "early", "late")
    assert result.failures[0].name == "broken"
    assert result.failures[0].error_type == "RuntimeError"
    assert calls == ["early", "late"]


def test_hook_payload_is_read_only_and_capacity_is_bounded():
    registry = LifecycleHookRegistry(max_hooks=1)

    def mutate(event, payload):
        payload["x"] = 1

    registry.register("one", "event", mutate)
    with pytest.raises(HookRegistrationError, match="capacity"):
        registry.register("two", "event", lambda *_: None)
    result = registry.dispatch("event", {"x": 0})
    assert result.failures[0].error_type == "TypeError"
