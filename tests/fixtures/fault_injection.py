"""Deterministic fault doubles shared by reliability tests.

The helpers model failures at existing dependency seams.  They never sleep,
open sockets, alter process-wide limits, or write outside pytest paths.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from threading import Event, Lock
from typing import Any, Callable, Iterable


@dataclass(frozen=True, slots=True)
class Return:
    """One scripted call result, including exception objects as plain values."""

    value: Any


@dataclass(frozen=True, slots=True)
class Raise:
    """One scripted call failure."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class Invoke:
    """One scripted callback for state transitions at an exact call boundary."""

    callback: Callable[..., Any]


class ScriptedCall:
    """Thread-safe, finite callable with an auditable call ledger."""

    def __init__(self, outcomes: Iterable[Return | Raise | Invoke]) -> None:
        self._outcomes = list(outcomes)
        self._lock = Lock()
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            call_number = len(self.calls) + 1
            self.calls.append((args, dict(kwargs)))
            if not self._outcomes:
                raise AssertionError(
                    f"unexpected scripted call {call_number}; no outcomes remain"
                )
            outcome = self._outcomes.pop(0)
        if isinstance(outcome, Raise):
            raise outcome.error
        if isinstance(outcome, Invoke):
            return outcome.callback(*args, **kwargs)
        return outcome.value

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self._outcomes)


class MutableCancellationToken:
    """Event-backed token that tests can flip inside a scripted dependency."""

    def __init__(self, *, cancelled: bool = False) -> None:
        self._event = Event()
        if cancelled:
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


@dataclass(slots=True)
class _ArmedSQLiteFault:
    operation: str
    error: BaseException
    statement_contains: str | None = None


class _FaultingSQLiteConnection:
    def __init__(self, owner: "SQLiteFaultConnector", connection: sqlite3.Connection):
        self._owner = owner
        self._connection = connection

    def execute(self, statement: str, *args: Any, **kwargs: Any):
        self._owner._raise_if_armed("execute", statement)
        return self._connection.execute(statement, *args, **kwargs)

    def executescript(self, statement: str):
        self._owner._raise_if_armed("executescript", statement)
        return self._connection.executescript(statement)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args: Any):
        return self._connection.__exit__(*args)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class SQLiteFaultConnector:
    """Real SQLite connector with one-shot statement-level fault injection."""

    def __init__(self, connector: Callable[..., sqlite3.Connection] = sqlite3.connect):
        self._connector = connector
        self._faults: list[_ArmedSQLiteFault] = []
        self._lock = Lock()
        self.calls = 0

    def fail_next(
        self,
        operation: str,
        error: BaseException,
        *,
        statement_contains: str | None = None,
    ) -> None:
        if operation not in {"connect", "execute", "executescript"}:
            raise ValueError("unsupported SQLite fault operation")
        with self._lock:
            self._faults.append(
                _ArmedSQLiteFault(operation, error, statement_contains)
            )

    def __call__(self, *args: Any, **kwargs: Any):
        self._raise_if_armed("connect")
        with self._lock:
            self.calls += 1
        return _FaultingSQLiteConnection(
            self, self._connector(*args, **kwargs)
        )

    def _raise_if_armed(self, operation: str, statement: str = "") -> None:
        with self._lock:
            for index, fault in enumerate(self._faults):
                if fault.operation != operation:
                    continue
                if (
                    fault.statement_contains is not None
                    and fault.statement_contains not in statement
                ):
                    continue
                self._faults.pop(index)
                error = fault.error
                break
            else:
                return
        raise error


class ScriptedProcess:
    """Small Popen-shaped process with exact wait and pipe behavior."""

    def __init__(
        self,
        *,
        pid: int = 701,
        waits: Iterable[Return | Raise | Invoke] = (Return(0),),
        stdout: Any = None,
        stderr: Any = None,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.stdout = stdout
        self.stderr = stderr
        self.killed = False
        self._wait = ScriptedCall(waits)

    def wait(self, timeout: float | None = None) -> int:
        result = self._wait(timeout=timeout)
        self.returncode = int(result)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


__all__ = [
    "Invoke",
    "MutableCancellationToken",
    "Raise",
    "Return",
    "SQLiteFaultConnector",
    "ScriptedCall",
    "ScriptedProcess",
]
