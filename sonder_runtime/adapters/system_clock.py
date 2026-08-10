"""System clock adapter (SPEC-5 WP11)."""
from __future__ import annotations

import time


class SystemClock:
    def now_utc_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def monotonic(self) -> float:
        return time.monotonic()
