r"""Read the user's short-date order so ``schtasks`` dates are not misread.

``schtasks`` prints dates in the user's regional format, so ``12/01/2027`` is
12 January for some users and 1 December for others. Windows records the
pattern in ``HKCU\Control Panel\International\sShortDate``. ``winreg`` exists
only on Windows; elsewhere (or on any registry failure) the order is UNKNOWN
and the domain parser refuses ambiguous dates.
"""
from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from types import ModuleType

from sonder_runtime.domain.health_alerts import DateOrder

winreg: ModuleType | None
try:  # pragma: no cover - import guard, exercised per platform
    winreg = importlib.import_module("winreg")
except ImportError:  # pragma: no cover
    winreg = None

_INTERNATIONAL_KEY = r"Control Panel\International"
_SHORT_DATE_VALUE = "sShortDate"
_FIELD_RUN = re.compile(r"d+|M+|y+")
_ORDERS = {"dMy": DateOrder.DMY, "Mdy": DateOrder.MDY, "yMd": DateOrder.YMD}

ShortDateReader = Callable[[], "str | None"]


def read_short_date_pattern() -> str | None:
    """The raw ``sShortDate`` pattern, or None when it cannot be read."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNATIONAL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, _SHORT_DATE_VALUE)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def date_order_from_pattern(pattern: str | None) -> DateOrder:
    """Map ``dd/MM/yyyy``-style patterns to a :class:`DateOrder`; anything else is UNKNOWN."""
    if not isinstance(pattern, str):
        return DateOrder.UNKNOWN
    letters = "".join(dict.fromkeys(run[0] for run in _FIELD_RUN.findall(pattern)))
    return _ORDERS.get(letters, DateOrder.UNKNOWN)


class ShortDateOrderReader:
    """Callable returning the date order; the registry reader is injectable for tests."""

    def __init__(self, read_pattern: ShortDateReader = read_short_date_pattern) -> None:
        self._read_pattern = read_pattern

    def __call__(self) -> DateOrder:
        try:
            return date_order_from_pattern(self._read_pattern())
        except Exception:  # noqa: BLE001 - an unreadable setting must degrade to UNKNOWN
            return DateOrder.UNKNOWN


__all__ = ["ShortDateOrderReader", "date_order_from_pattern", "read_short_date_pattern"]
