"""Read a scheduled task's last run and result with ``schtasks`` (``Get-ScheduledTask`` is unusable here)."""
from __future__ import annotations

import locale
import re
import subprocess
from collections.abc import Callable

from sonder_runtime.application.ports.health_alerts import TaskUnavailable
from sonder_runtime.domain.health_alerts import (
    ScheduledTaskState,
    TaskParseError,
    parse_schtasks_csv,
)

_TASK_NAME = re.compile(r"[\w .\\-]{1,200}")
ProcessRunner = Callable[..., "subprocess.CompletedProcess[str]"]


class SchtasksReader:
    def __init__(self, *, timeout_seconds: float, run: ProcessRunner = subprocess.run) -> None:
        self._timeout = timeout_seconds
        self._run = run

    def read(self, name: str) -> ScheduledTaskState:
        if not _TASK_NAME.fullmatch(name) or name.startswith("-"):
            raise TaskUnavailable("task name is not a plain task path")
        argv = ["schtasks", "/query", "/tn", name, "/fo", "csv", "/v"]
        try:
            done = self._run(
                argv, capture_output=True, text=True, check=False,
                encoding=locale.getpreferredencoding(False), errors="replace",
                timeout=self._timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TaskUnavailable("schtasks could not run: %s" % type(exc).__name__) from exc
        if done.returncode != 0:
            raise TaskUnavailable("schtasks exit %d (task missing or access denied)" % done.returncode)
        try:
            return parse_schtasks_csv(done.stdout or "", name)
        except TaskParseError as exc:
            raise TaskUnavailable(str(exc)) from exc


__all__ = ["SchtasksReader"]
