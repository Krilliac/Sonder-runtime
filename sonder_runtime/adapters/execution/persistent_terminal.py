"""Durable terminal adapter for the typed execution-world ports (EXEC-003).

The adapter owns one local subprocess per terminal and persists the terminal
identity, dimensions, lifecycle state, and bounded output journal in SQLite.
The process remains an adapter-owned resource; a handle is only a capability.
Reconnect is therefore allowed while the owning adapter still has the live
process, and it fails closed if durable metadata outlives that process.
"""
from __future__ import annotations

from contextlib import contextmanager

from sonder_runtime.adapters.persistence.owned_sqlite import transaction as owned_sqlite_transaction

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from typing import Callable, Protocol

from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.execution.world_control import (
    OutputEvent,
    OutputPage,
    OutputStream,
    OutputWatermark,
)
from sonder_runtime.application.ports.execution_world import (
    CleanupResult,
    ExecutionWorldState,
    TerminalChunk,
    TerminalHandle,
    TerminalRequest,
    TerminalService,
)


class PersistentTerminalError(RuntimeError):
    """The durable terminal state cannot support the requested operation."""


class TerminalCleanupError(PersistentTerminalError):
    """A terminal could not be proven quiescent within the cleanup bound."""


@dataclass(frozen=True)
class TerminalCleanup:
    quiescent: bool
    active_resources: int


class _ProcessLike(Protocol):
    pid: int
    stdin: object | None
    stdout: object | None
    stderr: object | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_DDL = """
CREATE TABLE IF NOT EXISTS terminal_sessions (
    terminal_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL,
    status TEXT NOT NULL,
    columns INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    pid INTEGER,
    created_at TEXT NOT NULL,
    stopped_at TEXT
);
CREATE TABLE IF NOT EXISTS terminal_output (
    terminal_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    stream TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (terminal_id, sequence),
    FOREIGN KEY (terminal_id) REFERENCES terminal_sessions(terminal_id)
);
"""


class _SQLiteTerminalJournal:
    def __init__(self, db_path: str | Path, *, max_events: int, max_bytes: int):
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(_DDL)

    @contextmanager
    def _connect(self):
        with owned_sqlite_transaction(str(self.path), timeout=5.0) as connection:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection

    def create(self, terminal_id: str, world_id: str, columns: int, rows: int, pid: int) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO terminal_sessions(terminal_id,world_id,status,columns,rows,pid,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (terminal_id, world_id, "active", columns, rows, pid, _now()),
            )

    def replace_stopped(self, terminal_id: str, world_id: str, columns: int, rows: int, pid: int) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM terminal_output WHERE terminal_id=?", (terminal_id,)
            )
            connection.execute(
                "UPDATE terminal_sessions SET world_id=?,status='active',columns=?,rows=?,pid=?,"
                "created_at=?,stopped_at=NULL WHERE terminal_id=?",
                (world_id, columns, rows, pid, _now(), terminal_id),
            )

    def session(self, terminal_id: str) -> tuple[object, ...]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT terminal_id,world_id,status,columns,rows,pid FROM terminal_sessions WHERE terminal_id=?",
                (terminal_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown terminal {terminal_id!r}")
        return row

    def mark_stopped(self, terminal_id: str) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                "UPDATE terminal_sessions SET status='stopped',stopped_at=? WHERE terminal_id=?",
                (_now(), terminal_id),
            )

    def resize(self, terminal_id: str, columns: int, rows: int) -> None:
        with self.lock, self._connect() as connection:
            connection.execute(
                "UPDATE terminal_sessions SET columns=?,rows=? WHERE terminal_id=? AND status='active'",
                (columns, rows, terminal_id),
            )

    def append(self, terminal_id: str, stream: str, data: str) -> int:
        encoded_size = len(data.encode("utf-8"))
        if encoded_size > self.max_bytes:
            data = data.encode("utf-8")[: self.max_bytes].decode("utf-8", "ignore")
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM terminal_output WHERE terminal_id=?",
                (terminal_id,),
            ).fetchone()
            sequence = int(row[0]) + 1
            connection.execute(
                "INSERT INTO terminal_output(terminal_id,sequence,stream,data) VALUES(?,?,?,?)",
                (terminal_id, sequence, stream, data),
            )
            rows = connection.execute(
                "SELECT sequence,data FROM terminal_output WHERE terminal_id=? ORDER BY sequence",
                (terminal_id,),
            ).fetchall()
            total = sum(len(str(item[1]).encode("utf-8")) for item in rows)
            while len(rows) > self.max_events or total > self.max_bytes:
                old_sequence, old_data = rows.pop(0)
                total -= len(str(old_data).encode("utf-8"))
                connection.execute(
                    "DELETE FROM terminal_output WHERE terminal_id=? AND sequence=?",
                    (terminal_id, old_sequence),
                )
            return sequence

    def page(
        self,
        terminal_id: str,
        after: OutputWatermark | None,
        *,
        max_events: int,
        max_bytes: int,
    ) -> OutputPage:
        if type(max_events) is not int or max_events < 1:
            raise ValueError("max_events must be positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        cursor = after or OutputWatermark(0)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence,stream,data FROM terminal_output WHERE terminal_id=? AND sequence>? "
                "ORDER BY sequence",
                (terminal_id, cursor.sequence),
            ).fetchall()
            first = connection.execute(
                "SELECT MIN(sequence) FROM terminal_output WHERE terminal_id=?",
                (terminal_id,),
            ).fetchone()[0]
        truncated = first is not None and cursor.sequence < int(first) - 1
        selected: list[OutputEvent] = []
        used = 0
        for sequence, stream, data in rows:
            size = len(str(data).encode("utf-8"))
            if selected and (len(selected) >= max_events or used + size > max_bytes):
                break
            selected.append(OutputEvent(OutputWatermark(int(sequence)), OutputStream(stream), str(data)))
            used += size
            if len(selected) >= max_events or used >= max_bytes:
                break
        last = selected[-1].watermark if selected else cursor
        has_more = any(int(row[0]) > last.sequence for row in rows)
        return OutputPage(tuple(selected), last, has_more, truncated)


class _SubprocessTerminalHandle:
    def __init__(self, service: "SQLitePersistentTerminalService", terminal_id: str, process: _ProcessLike):
        self._service = service
        self._terminal_id = terminal_id
        self._process = process
        self._cursor = OutputWatermark(0)
        self._closed = False

    @property
    def resource_id(self) -> str:
        return self._terminal_id

    @property
    def world_id(self) -> str:
        return self._service.world_id

    def send(self, data: str) -> None:
        if self._closed:
            raise PersistentTerminalError("terminal handle is closed")
        self._service._send(self._terminal_id, self._process, data)

    def read(self, *, max_chunks: int = 64) -> tuple[TerminalChunk, ...]:
        if self._closed:
            raise PersistentTerminalError("terminal handle is closed")
        page = self._service.read_page(
            self._terminal_id, after=self._cursor, max_events=max_chunks, max_bytes=self._service.max_read_bytes
        )
        self._cursor = page.next_watermark
        return tuple(TerminalChunk(event.stream.value, event.data, event.watermark.sequence) for event in page.events)

    def resize(self, *, columns: int, rows: int) -> None:
        if self._closed:
            raise PersistentTerminalError("terminal handle is closed")
        self._service._resize(self._terminal_id, self._process, columns, rows)

    def cancel(self, *, reason: str = "cancellation requested") -> None:
        if self._closed:
            return
        if not self._service.stop(self._terminal_id, reason=reason):
            raise TerminalCleanupError("terminal cancellation did not prove cleanup")

    def close(self) -> None:
        if self._closed:
            return
        if not self._service.stop(self._terminal_id, reason="handle closed"):
            raise TerminalCleanupError("terminal close did not prove cleanup")
        self._closed = True


class SQLitePersistentTerminalService(TerminalService):
    """SQLite-backed local terminal service with bounded durable output."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        world_id: str,
        max_events: int = 256,
        max_bytes: int = 64 * 1024,
        max_read_bytes: int = 16 * 1024,
        popen_factory: Callable[..., _ProcessLike] | None = None,
    ) -> None:
        if not world_id.strip():
            raise ValueError("world_id must be non-empty")
        if type(max_read_bytes) is not int or max_read_bytes < 1:
            raise ValueError("max_read_bytes must be positive")
        self.world_id = world_id
        self.max_read_bytes = max_read_bytes
        self._journal = _SQLiteTerminalJournal(
            db_path, max_events=max_events, max_bytes=max_bytes
        )
        self._popen = popen_factory or subprocess.Popen
        self._processes: dict[str, _ProcessLike] = {}
        self._lock = threading.RLock()
        self._closing = False

    def open(self, request: TerminalRequest, context: OperationContext) -> TerminalHandle:
        if self._closing:
            raise PersistentTerminalError("terminal service is closing")
        if context.expired or context.cancellation.cancelled:
            raise PersistentTerminalError("terminal operation is cancelled or expired")
        if not request.argv or any(not isinstance(item, str) or not item for item in request.argv):
            raise ValueError("terminal argv must be non-empty strings")
        if request.columns < 1 or request.rows < 1:
            raise ValueError("terminal dimensions must be positive")
        with self._lock:
            process = self._start(request)
            # The low-level TerminalService port has no id field, so generate
            # an owner-stable id from the process identity and expose
            # ``open_named`` for application callers that already have one.
            terminal_id = f"terminal-{process.pid}"
            self._journal.create(terminal_id, self.world_id, request.columns, request.rows, process.pid)
            self._processes[terminal_id] = process
            self._start_reader(terminal_id, process, "stdout", process.stdout)
            self._start_reader(terminal_id, process, "stderr", process.stderr)
            return _SubprocessTerminalHandle(self, terminal_id, process)

    def open_named(self, terminal_id: str, request: TerminalRequest, context: OperationContext) -> TerminalHandle:
        """Open a caller-stable id while retaining the typed TerminalService port."""
        if not terminal_id.strip():
            raise ValueError("terminal_id must be non-empty")
        if self._closing:
            raise PersistentTerminalError("terminal service is closing")
        if context.expired or context.cancellation.cancelled:
            raise PersistentTerminalError("terminal operation is cancelled or expired")
        if not request.argv or any(not isinstance(item, str) or not item for item in request.argv):
            raise ValueError("terminal argv must be non-empty strings")
        if request.columns < 1 or request.rows < 1:
            raise ValueError("terminal dimensions must be positive")
        with self._lock:
            try:
                row = self._journal.session(terminal_id)
                if row[2] == "active":
                    raise PersistentTerminalError("terminal already exists")
                process = self._start(request)
                self._journal.replace_stopped(terminal_id, self.world_id, request.columns, request.rows, process.pid)
            except KeyError:
                process = self._start(request)
                self._journal.create(terminal_id, self.world_id, request.columns, request.rows, process.pid)
            self._processes[terminal_id] = process
            self._start_reader(terminal_id, process, "stdout", process.stdout)
            self._start_reader(terminal_id, process, "stderr", process.stderr)
            return _SubprocessTerminalHandle(self, terminal_id, process)

    def reconnect(self, terminal_id: str) -> TerminalHandle:
        with self._lock:
            row = self._journal.session(terminal_id)
            if row[2] != "active":
                raise PersistentTerminalError("terminal is stopped")
            process = self._processes.get(terminal_id)
            if process is None or process.poll() is not None:
                self._journal.mark_stopped(terminal_id)
                raise TerminalCleanupError("durable terminal has no live owner")
            return _SubprocessTerminalHandle(self, terminal_id, process)

    def read_page(
        self,
        terminal_id: str,
        *,
        after: OutputWatermark | None = None,
        max_events: int = 64,
        max_bytes: int | None = None,
    ) -> OutputPage:
        row = self._journal.session(terminal_id)
        if row[2] not in {"active", "stopped"}:
            raise PersistentTerminalError("terminal state is not readable")
        return self._journal.page(
            terminal_id, after, max_events=max_events, max_bytes=max_bytes or self.max_read_bytes
        )

    def stop(self, terminal_id: str, *, reason: str = "stopped", timeout: float = 1.0) -> bool:
        del reason  # retained at the typed boundary; lifecycle state is authoritative here.
        with self._lock:
            process = self._processes.get(terminal_id)
            if process is None:
                row = self._journal.session(terminal_id)
                return row[2] == "stopped"
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=max(0.0, timeout))
                except subprocess.TimeoutExpired:
                    return False
            self._journal.mark_stopped(terminal_id)
            self._processes.pop(terminal_id, None)
            return True

    def cleanup(self, timeout: float | None = None) -> TerminalCleanup:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._lock:
            self._closing = True
            terminal_ids = tuple(self._processes)
        for terminal_id in terminal_ids:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if not self.stop(terminal_id, timeout=remaining if remaining is not None else 1.0):
                return TerminalCleanup(False, len(self._processes))
        return TerminalCleanup(not self._processes, len(self._processes))

    def _start(self, request: TerminalRequest) -> _ProcessLike:
        environment = dict(request.environment)
        if request.cwd is not None:
            cwd = os.fspath(request.cwd)
        else:
            cwd = None
        try:
            return self._popen(
                tuple(request.argv),
                cwd=cwd,
                env=environment or None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except Exception as exc:
            raise PersistentTerminalError("terminal process could not be started") from exc

    def _start_reader(self, terminal_id: str, process: _ProcessLike, stream: str, pipe: object | None) -> None:
        if pipe is None:
            raise PersistentTerminalError("terminal process did not expose output")

        def read_loop() -> None:
            try:
                while True:
                    chunk = pipe.read(4096)  # type: ignore[attr-defined]
                    if not chunk:
                        return
                    if isinstance(chunk, bytes):
                        chunk = chunk.decode("utf-8", "replace")
                    self._journal.append(terminal_id, stream, str(chunk))
            except (OSError, ValueError):
                # Process teardown closes pipes; no output is fabricated.
                return

        threading.Thread(target=read_loop, name=f"sonder-terminal-{terminal_id}-{stream}", daemon=True).start()

    def _send(self, terminal_id: str, process: _ProcessLike, data: str) -> None:
        if not isinstance(data, str):
            raise TypeError("terminal input must be text")
        if process.poll() is not None:
            self._journal.mark_stopped(terminal_id)
            raise TerminalCleanupError("terminal process has exited")
        try:
            if process.stdin is None:
                raise OSError("terminal stdin is unavailable")
            process.stdin.write(data.encode("utf-8"))  # type: ignore[attr-defined]
            process.stdin.flush()  # type: ignore[attr-defined]
        except (OSError, ValueError) as exc:
            raise TerminalCleanupError("terminal input could not be delivered") from exc

    def _resize(self, terminal_id: str, process: _ProcessLike, columns: int, rows: int) -> None:
        if columns < 1 or rows < 1:
            raise ValueError("terminal dimensions must be positive")
        if process.poll() is not None:
            self._journal.mark_stopped(terminal_id)
            raise TerminalCleanupError("terminal process has exited")
        self._journal.resize(terminal_id, columns, rows)


__all__ = [
    "PersistentTerminalError",
    "SQLitePersistentTerminalService",
    "TerminalCleanup",
    "TerminalCleanupError",
]
