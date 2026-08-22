"""Small durable journal for bounded local repair execution."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class RepairJournal:
    """Atomic single-writer journal used by the local repair seam.

    The journal is intentionally separate from ``operations.db``: repair
    state is a decision ledger, while operation events are an audit stream.
    The application executor writes a ``pending`` record before effects and
    never retries a pending record after a crash without explicit recovery.
    """

    def __init__(self, path: str | os.PathLike[str], *, max_bytes: int = 1_048_576) -> None:
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")

    def get(self, repair_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read().get(repair_id)

    def put_if_absent(self, repair_id: str, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        with self._lock:
            data = self._read()
            existing = data.get(repair_id)
            if existing is not None:
                return existing, False
            data[repair_id] = record
            self._write(data)
            return record, True

    def replace(self, repair_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            if repair_id not in data:
                raise KeyError(repair_id)
            data[repair_id] = record
            self._write(data)

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        raw = self._path.read_bytes()
        if len(raw) > self._max_bytes:
            raise ValueError("repair journal exceeds configured bound")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("repair journal is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("repair journal must contain an object")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > self._max_bytes:
            raise ValueError("repair journal exceeds configured bound")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".repair-", dir=self._path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self._path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


__all__ = ["RepairJournal"]
