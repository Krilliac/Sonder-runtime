"""Immutable, deterministic policy for scoped provider replacement.

This module describes *which* provider a caller may use.  It does not create,
start, stop, or mutate providers.  A policy is a value: adding or removing an
override returns a new policy and leaves the source policy unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


def _name(value: object, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _scopes(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    try:
        result = tuple(_name(value, "scope") for value in values)
    except TypeError as exc:
        raise ValueError("scopes must be an iterable of names") from exc
    if len(set(result)) != len(result):
        raise ValueError("scopes must not contain duplicates")
    return result


@dataclass(frozen=True)
class ProviderOverride:
    """One replacement valid only inside one named scope."""

    scope: str
    provider: str
    replacement: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _name(self.scope, "scope"))
        object.__setattr__(self, "provider", _name(self.provider, "provider"))
        object.__setattr__(
            self, "replacement", _name(self.replacement, "replacement")
        )


@dataclass(frozen=True)
class ProviderOverridePolicy:
    """Base providers and scoped replacements, with no process-global state.

    ``scopes`` passed to :meth:`resolve` must be ordered from most specific to
    least specific.  That order is the complete precedence rule; it avoids
    relying on dictionary insertion order or registration timing.
    """

    providers: Mapping[str, str]
    overrides: tuple[ProviderOverride, ...] = ()

    def __post_init__(self) -> None:
        normalized = {
            _name(provider, "provider"): _name(value, "provider value")
            for provider, value in self.providers.items()
        }
        entries = tuple(self.overrides)
        keys = [(entry.scope, entry.provider) for entry in entries]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate scoped provider override")
        object.__setattr__(self, "providers", MappingProxyType(normalized))
        object.__setattr__(self, "overrides", entries)

    def with_override(
        self, scope: str, provider: str, replacement: str
    ) -> "ProviderOverridePolicy":
        entry = ProviderOverride(scope, provider, replacement)
        if (entry.scope, entry.provider) in {
            (item.scope, item.provider) for item in self.overrides
        }:
            raise ValueError(
                f"override already exists for scope {entry.scope!r} "
                f"and provider {entry.provider!r}"
            )
        return ProviderOverridePolicy(self.providers, self.overrides + (entry,))

    def without_override(
        self, scope: str, provider: str
    ) -> "ProviderOverridePolicy":
        scope_name = _name(scope, "scope")
        provider_name = _name(provider, "provider")
        return ProviderOverridePolicy(
            self.providers,
            tuple(
                item
                for item in self.overrides
                if (item.scope, item.provider) != (scope_name, provider_name)
            ),
        )

    def resolve(
        self, provider: str, scopes: object = None
    ) -> str:
        """Resolve a provider using the caller's explicit scope precedence."""
        provider_name = _name(provider, "provider")
        by_key = {
            (item.scope, item.provider): item.replacement for item in self.overrides
        }
        for scope in _scopes(scopes):
            replacement = by_key.get((scope, provider_name))
            if replacement is not None:
                return replacement
        try:
            return self.providers[provider_name]
        except KeyError as exc:
            raise KeyError(f"unknown provider {provider_name!r}") from exc


__all__ = ["ProviderOverride", "ProviderOverridePolicy"]
