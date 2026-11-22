"""Introspect callable signatures for optional keyword compatibility.
"""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def callable_accepts_keyword(callable_obj: Callable[..., object], name: str) -> bool:
    """Keep narrow test/extension doubles compatible with new optional seams."""
    try:
        parameters = inspect.signature(callable_obj).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.name == name
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
