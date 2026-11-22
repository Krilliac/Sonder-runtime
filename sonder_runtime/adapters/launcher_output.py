"""Pure output, timeout, and retention policy for the launcher."""
from __future__ import annotations

import threading
from typing import Any, Protocol

MAX_OPERATION_OUTPUT = 20_000
DEFAULT_OPERATION_RETENTION = 100
MAX_OPERATION_RETENTION = 1_000


def output_text(*values: object, limit: int = MAX_OPERATION_OUTPUT) -> str:
    """Join non-empty values and retain only the bounded output tail."""
    chunks: list[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip()
        if value:
            chunks.append(value)
    output = "\n".join(chunks)
    if len(output) <= limit:
        return output
    marker = "[output truncated]\n"
    return marker + output[-(limit - len(marker)):]


def bounded_seconds(value: Any, default: float, maximum: float) -> float:
    """Coerce a timeout into the launcher's inclusive safe range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(1.0, min(parsed, float(maximum)))


def retention_limit(
    value: Any,
    default: int = DEFAULT_OPERATION_RETENTION,
    maximum: int = MAX_OPERATION_RETENTION,
) -> int:
    """Coerce operation retention to a positive bounded integer."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


class _ReadablePipe(Protocol):
    def read(self, size: int = ..., /) -> bytes | str: ...


class BoundedOutputTail:
    """Thread-safe byte tail that never grows with command output volume."""

    def __init__(self, limit: int = MAX_OPERATION_OUTPUT) -> None:
        self.limit = max(256, int(limit))
        self.data = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes | bytearray | memoryview | str | None) -> None:
        if not chunk:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        with self.lock:
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[:len(self.data) - self.limit]
                self.truncated = True

    def text(self) -> str:
        with self.lock:
            value = bytes(self.data).decode("utf-8", errors="replace")
            truncated = self.truncated
        return ("[output truncated]\n" + value) if truncated else value


def drain_process_output(pipe: _ReadablePipe | None, tail: BoundedOutputTail) -> None:
    if pipe is None:
        return
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            tail.append(chunk)
    except (OSError, ValueError):
        return


__all__ = [
    "BoundedOutputTail",
    "DEFAULT_OPERATION_RETENTION",
    "MAX_OPERATION_OUTPUT",
    "MAX_OPERATION_RETENTION",
    "bounded_seconds",
    "drain_process_output",
    "output_text",
    "retention_limit",
]
