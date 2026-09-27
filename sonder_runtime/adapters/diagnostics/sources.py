"""Bounded text sources for the output digest: guarded files and job output.

``GuardedFileWindowSource`` reads only through ``log_inspect``'s guarded
no-follow window (allowed roots, sensitive-directory rejection, reparse-point
rejection, identity re-check after open, size/scan/time ceilings) and
additionally refuses credential stores and secret files before any byte is
read.

``RegistryJobOutputReader`` pages a durable job's retained output through the
registry's watermark stream under page, byte and wall-clock ceilings, keeping
a head plus a rolling tail so the returned window never exceeds its budget.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ...application.diagnostics.ports import TextWindow
from ...application.diagnostics.service import DigestSourceRejected
from ...application.execution.world_control import OutputWatermark
from ...domain.common.errors import DependencyUnavailable, NotFound
from ..filesystem import file_ops
from ..inspection import log_inspect


FILE_MAX_BYTES = log_inspect.DEFAULT_MAX_FILE_BYTES  # 64 MB
FILE_MAX_SCAN_BYTES = log_inspect.DEFAULT_MAX_SCAN_BYTES  # 4 MB
FILE_MAX_TIMEOUT_SECONDS = log_inspect.DEFAULT_TIMEOUT_SECONDS  # 5 s
FILE_MAX_LINES = log_inspect.HARD_MAX_LINES

JOB_MAX_PAGES = 400
JOB_MAX_SCAN_BYTES = 8 * 1024 * 1024
JOB_MAX_SECONDS = 5.0
JOB_PAGE_EVENTS = 256
JOB_PAGE_BYTES = 65_536
JOB_HARD_MAX_BYTES = 2_000_000
JOB_HARD_HEAD_BYTES = 65_536


def _secret_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in {item.lower() for item in file_ops.SECRET_FILES}
        or any(lowered.endswith(suffix) for suffix in file_ops.SECRET_SUFFIXES)
        or lowered == ".env" or lowered == ".envrc" or lowered.startswith(".env.")
    )


def _display_label(target: Path) -> str:
    try:
        root = file_ops.workspace_root().resolve()
        return target.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return target.name


class GuardedFileWindowSource:
    """``TextWindowSource`` over ``log_inspect.read_guarded_text_window``."""

    def __init__(
        self,
        *,
        reader: Callable[..., tuple[list[str], dict, Path]] | None = None,
        resolver: Callable[..., Path] | None = None,
    ) -> None:
        self._reader = reader or log_inspect.read_guarded_text_window
        self._resolver = resolver or log_inspect.resolve_log_path

    def read_file_window(
        self,
        path: str,
        *,
        extra_roots: str,
        max_scan_bytes: int,
        tail_lines: int,
        timeout_seconds: float,
    ) -> TextWindow:
        try:
            target = self._resolver(path, extra_roots=extra_roots)
        except (log_inspect.LogInspectError, OSError, ValueError) as exc:
            raise DigestSourceRejected("DIGEST_SOURCE_REJECTED: path rejected") from exc
        self._refuse_secret(target)
        try:
            lines, window, opened = self._reader(
                path,
                extra_roots=extra_roots,
                max_file_bytes=FILE_MAX_BYTES,
                max_scan_bytes=max(1, min(int(max_scan_bytes), FILE_MAX_SCAN_BYTES)),
                tail_lines=max(0, min(int(tail_lines), FILE_MAX_LINES)),
                max_lines=FILE_MAX_LINES,
                timeout=max(0.05, min(float(timeout_seconds), FILE_MAX_TIMEOUT_SECONDS)),
            )
        except (log_inspect.LogInspectError, OSError, ValueError) as exc:
            raise DigestSourceRejected("DIGEST_SOURCE_REJECTED: unreadable") from exc
        # The opened handle's own path is re-checked: a swap between the
        # resolve above and the open cannot smuggle a credential store in.
        self._refuse_secret(Path(opened))
        return TextWindow(
            text="\n".join(lines),
            label=_display_label(Path(opened)),
            bytes_read=int(window.get("bytes_read", 0)),
            source_bytes=int(window.get("source_bytes", 0)),
            truncated=bool(window.get("byte_truncated") or window.get("line_cap_truncated")),
        )

    @staticmethod
    def _refuse_secret(target: Path) -> None:
        if file_ops.credential_read_component(target):
            raise DigestSourceRejected("DIGEST_SOURCE_REJECTED: credential store")
        if _secret_name(target.name):
            raise DigestSourceRejected("DIGEST_SOURCE_REJECTED: secret file")


class RegistryJobOutputReader:
    """``JobOutputReader`` over a durable job registry's watermark stream."""

    def __init__(
        self,
        registry_getter: Callable[[], Any],
        *,
        monotonic: Callable[[], float] = time.monotonic,
        max_pages: int = JOB_MAX_PAGES,
        max_scan_bytes: int = JOB_MAX_SCAN_BYTES,
        max_seconds: float = JOB_MAX_SECONDS,
    ) -> None:
        if not callable(registry_getter):
            raise TypeError("registry_getter must be callable")
        self._registry_getter = registry_getter
        self._monotonic = monotonic
        self._max_pages = max(1, min(int(max_pages), JOB_MAX_PAGES))
        self._max_scan_bytes = max(1, min(int(max_scan_bytes), JOB_MAX_SCAN_BYTES))
        self._max_seconds = max(0.01, min(float(max_seconds), JOB_MAX_SECONDS))

    def _registry(self) -> Any:
        registry = self._registry_getter()
        if registry is None:
            raise DependencyUnavailable("job registry is not available")
        return registry

    def job_metadata(self, job_id: str) -> Mapping[str, str] | None:
        registry = self._registry()
        view = getattr(registry, "view", None)
        try:
            if callable(view):
                snapshot = view(job_id)
                record = snapshot.record
                raw = dict(snapshot.metadata or {})
            else:
                record = registry.get(job_id)
                if record is None:
                    return None
                raw = {}
        except KeyError:
            return None
        metadata = {
            str(key): value if isinstance(value, str) else str(value)
            for key, value in raw.items()
            if isinstance(key, str)
        }
        metadata["kind"] = record.identity.kind
        metadata["status"] = getattr(record.status, "value", str(record.status))
        return metadata

    def read_output(
        self, job_id: str, *, max_bytes: int = JOB_HARD_MAX_BYTES,
        head_bytes: int = JOB_HARD_HEAD_BYTES,
    ) -> TextWindow:
        registry = self._registry()
        stream = getattr(registry, "stream", None)
        if not callable(stream):
            raise DependencyUnavailable("job registry cannot stream output")
        max_bytes = max(1, min(int(max_bytes), JOB_HARD_MAX_BYTES))
        head_budget = max(0, min(int(head_bytes), JOB_HARD_HEAD_BYTES, max_bytes))
        tail_budget = max_bytes - head_budget
        head: list[str] = []
        head_used = 0
        tail: deque[tuple[str, int]] = deque()
        tail_used = 0
        scanned = 0
        truncated = False
        middle_dropped = False
        head_full = head_budget == 0
        cursor = OutputWatermark(0)
        deadline = self._monotonic() + self._max_seconds
        pages = 0
        while True:
            if pages >= self._max_pages or scanned >= self._max_scan_bytes:
                truncated = True
                break
            if self._monotonic() >= deadline:
                truncated = True
                break
            try:
                page = stream(
                    job_id, after=cursor, max_events=JOB_PAGE_EVENTS,
                    max_bytes=JOB_PAGE_BYTES,
                )
            except KeyError as exc:
                raise NotFound("job not found") from exc
            pages += 1
            if page.truncated:
                # The registry's bounded retention dropped output before the
                # cursor: the window cannot claim to be the whole stream.
                truncated = True
            for event in page.events:
                data = event.data
                if getattr(event, "spill", None) is not None:
                    truncated = True
                size = len(data.encode("utf-8", errors="replace"))
                scanned += size
                if not head_full:
                    room = head_budget - head_used
                    if size <= room:
                        head.append(data)
                        head_used += size
                        continue
                    piece = _utf8_prefix(data, room)
                    head.append(piece)
                    head_used += len(piece.encode("utf-8", errors="replace"))
                    data = data[len(piece):]
                    size = len(data.encode("utf-8", errors="replace"))
                    head_full = True
                if not data:
                    continue
                if size > tail_budget:
                    data = _utf8_suffix(data, tail_budget)
                    size = len(data.encode("utf-8", errors="replace"))
                    middle_dropped = True
                tail.append((data, size))
                tail_used += size
                while tail_used > tail_budget and tail:
                    _, dropped = tail.popleft()
                    tail_used -= dropped
                    middle_dropped = True
            if page.events:
                cursor = page.next_watermark
            if not page.has_more or not page.events:
                break
        parts = ["".join(head)]
        if middle_dropped:
            parts.append("\n")
        parts.append("".join(item for item, _ in tail))
        text = "".join(parts)
        return TextWindow(
            text=text,
            label="job:%s" % job_id,
            bytes_read=head_used + tail_used,
            source_bytes=scanned,
            truncated=bool(truncated or middle_dropped),
        )


def _utf8_prefix(text: str, budget: int) -> str:
    encoded = text.encode("utf-8", errors="replace")[: max(0, budget)]
    return encoded.decode("utf-8", errors="ignore")


def _utf8_suffix(text: str, budget: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if budget <= 0:
        return ""
    return encoded[-budget:].decode("utf-8", errors="ignore")


__all__ = ["GuardedFileWindowSource", "RegistryJobOutputReader"]
