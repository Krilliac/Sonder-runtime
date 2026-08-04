"""Clock port — testable time (SPEC-3 section 4)."""
from __future__ import annotations

from typing import Protocol


class Clock(Protocol):
    def now_utc_iso(self) -> str: ...

    def monotonic(self) -> float: ...
