"""A small, deterministic lifecycle hook registry for extensions and agents.

Hooks are observers. Their failures are reported in the dispatch result and do
not change the lifecycle operation that emitted the event.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
import re
from typing import Any


_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
MAX_HOOKS = 256
MAX_PAYLOAD_FIELDS = 256


class HookRegistrationError(ValueError):
    """Raised when a hook registration or dispatch input is invalid."""


@dataclass(frozen=True, slots=True)
class _Registration:
    name: str
    event: str
    priority: int
    callback: Callable[[str, Mapping[str, Any]], object]


@dataclass(frozen=True, slots=True)
class HookFailure:
    name: str
    error_type: str


@dataclass(frozen=True, slots=True)
class HookDispatchResult:
    event: str
    invoked: tuple[str, ...]
    failures: tuple[HookFailure, ...]


class LifecycleHookRegistry:
    """Register bounded observers and dispatch them in deterministic order."""

    def __init__(self, *, max_hooks: int = MAX_HOOKS) -> None:
        if not 1 <= max_hooks <= MAX_HOOKS:
            raise HookRegistrationError("max_hooks is out of bounds")
        self._max_hooks = max_hooks
        self._hooks: dict[str, _Registration] = {}

    def register(
        self,
        name: str,
        event: str,
        callback: Callable[[str, Mapping[str, Any]], object],
        *,
        priority: int = 0,
    ) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise HookRegistrationError("hook name is invalid")
        if not isinstance(event, str) or not _NAME.fullmatch(event):
            raise HookRegistrationError("hook event is invalid")
        if not callable(callback):
            raise HookRegistrationError("hook callback must be callable")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise HookRegistrationError("hook priority must be an integer")
        if name not in self._hooks and len(self._hooks) >= self._max_hooks:
            raise HookRegistrationError("hook registry capacity exhausted")
        self._hooks[name] = _Registration(name, event, priority, callback)

    def unregister(self, name: str) -> None:
        self._hooks.pop(name, None)

    def dispatch(self, event: str, payload: Mapping[str, Any] | None = None) -> HookDispatchResult:
        if not isinstance(event, str) or not _NAME.fullmatch(event):
            raise HookRegistrationError("hook event is invalid")
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            raise HookRegistrationError("hook payload must be an object")
        if len(payload) > MAX_PAYLOAD_FIELDS:
            raise HookRegistrationError("hook payload has too many fields")
        safe_payload = MappingProxyType(dict(payload))
        registrations = sorted(
            (item for item in self._hooks.values() if item.event == event),
            key=lambda item: (-item.priority, item.name),
        )
        invoked: list[str] = []
        failures: list[HookFailure] = []
        for registration in registrations:
            invoked.append(registration.name)
            try:
                registration.callback(event, safe_payload)
            except Exception as exc:  # observer isolation is the contract
                failures.append(HookFailure(registration.name, type(exc).__name__))
        return HookDispatchResult(event, tuple(invoked), tuple(failures))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._hooks))

    def __len__(self) -> int:
        return len(self._hooks)


__all__ = [
    "HookDispatchResult", "HookFailure", "HookRegistrationError",
    "LifecycleHookRegistry", "MAX_HOOKS", "MAX_PAYLOAD_FIELDS",
]
