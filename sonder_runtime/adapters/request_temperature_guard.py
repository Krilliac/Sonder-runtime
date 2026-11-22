"""Entry-point guard for a caller-supplied request temperature.

The tool entry points report model-call failures as typed ``ModelCallError``
values that the server renders as an ``ERROR: ...`` string. A refused
temperature uses the same kind (``configuration``) as ``sonder()`` does, so
every surface reports it identically and it never escapes as a raw exception.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.request_temperature import validate_request_temperature

_T = TypeVar("_T")


def require_valid_temperature(value: _T) -> _T:
    """Return ``value`` if acceptable, else raise ``ModelCallError``."""
    try:
        return validate_request_temperature(value)
    except InvalidInput as error:
        raise ModelCallError("configuration", str(error)) from error


def refusal(value: object, render: Callable[[ModelCallError], str]) -> str | None:
    """Return ``render(ModelCallError)`` text for a bad temperature, else None."""
    try:
        require_valid_temperature(value)
    except ModelCallError as error:
        return render(error)
    return None
