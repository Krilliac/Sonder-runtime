"""JSON file holding when each problem was last announced, and the wall clock."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonAlertStateStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self) -> dict[str, float]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.error("health alert state file unreadable; treating as empty")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            k: float(v) for k, v in raw.items()
            if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool)
        }

    def save(self, sent: dict[str, float]) -> None:
        """Atomically replace the file; an I/O failure is logged, never raised."""
        tmp_name: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=self._path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(sent, sort_keys=True))
            os.replace(tmp_name, self._path)
            tmp_name = None
        except OSError as exc:
            logger.warning("health alert state not saved: %s", type(exc).__name__)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass


class SystemHealthClock:
    def now_local(self) -> datetime:
        return datetime.now()

    def epoch(self) -> float:
        return time.time()


__all__ = ["JsonAlertStateStore", "SystemHealthClock"]
