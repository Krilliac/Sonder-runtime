"""Pure normalization for the ``sonder doctor`` check registry."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any


CheckCallable = Callable[[], Any]


def iter_specs(
    checks: Mapping[str, CheckCallable] | Iterable[Any],
) -> list[tuple[str, CheckCallable]]:
    """Normalize supported check registries into ordered name/callable pairs."""
    specs: list[tuple[str, CheckCallable]] = []
    if isinstance(checks, Mapping):
        for name, fn in checks.items():
            specs.append((str(name), fn))
        return specs
    for index, item in enumerate(checks):
        if isinstance(item, tuple) and len(item) == 2:
            name, fn = item
            specs.append((str(name), fn))
        elif callable(item):
            name = getattr(item, "name", None) or getattr(
                item, "__name__", None
            )
            specs.append((str(name or "check_%d" % index), item))
        else:
            raise TypeError(
                "check spec must be a (name, callable) pair or a callable, "
                "got %r" % (item,)
            )
    return specs


__all__ = ["CheckCallable", "iter_specs"]
