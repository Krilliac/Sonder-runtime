"""Admission for concurrent calls to one tool graph.

Only positively declared concurrency-safe calls may overlap. An unknown or
unsafe call excludes all other calls, including otherwise safe readers.
"""
from __future__ import annotations

from contextlib import contextmanager
from threading import Condition, RLock, get_ident

from ...domain.common.errors import Forbidden


class ToolConcurrencyGate:
    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._readers: dict[int, int] = {}
        self._writer: int | None = None
        self._depth = 0

    @contextmanager
    def admit(self, parallel: bool, check_control=lambda: None):
        owner = get_ident()
        with self._condition:
            if self._writer == owner:
                self._depth += 1
                exclusive = True
            elif not parallel:
                # Never deadlock two recursive read-to-write upgrades.
                if owner in self._readers and len(self._readers) > 1:
                    raise Forbidden("nested tool cannot widen concurrent execution")
                while self._writer is not None or any(key != owner for key in self._readers):
                    check_control()
                    self._condition.wait(0.05)
                self._writer = owner
                self._depth = 1
                exclusive = True
            else:
                while self._writer is not None:
                    check_control()
                    self._condition.wait(0.05)
                self._readers[owner] = self._readers.get(owner, 0) + 1
                exclusive = False
        try:
            check_control()
            yield
        finally:
            with self._condition:
                if exclusive:
                    self._depth -= 1
                    if self._depth == 0:
                        self._writer = None
                else:
                    self._readers[owner] -= 1
                    if self._readers[owner] == 0:
                        del self._readers[owner]
                self._condition.notify_all()
