"""A real structured-test-run stack for the real-process tests.

The durable pieces are the production ones (SQLite job registry, subprocess
job provider with process-tree supervision, planner, launcher, collector,
service). The inventory lookup, the host-executable guard, the job output
reader and the output summariser stand in for lanes that compose them in the
runtime (host tool inventory, diagnostics); each double is minimal and
faithful to the port it replaces. ``tests/test_developer_tool_wiring.py``
exercises the runtime's own composition.
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.adapters.testing.artifacts import ReportArtifactCollector
from sonder_runtime.adapters.testing.detection import ProjectTestPlanner
from sonder_runtime.adapters.testing.launcher import ProcessTestLauncher
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.world_control import OutputWatermark
from sonder_runtime.application.testing.service import TestRunService

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class HostRecord:
    name: str
    path: str
    version: str


class HostLookup:
    """``HostToolLookup`` double: ``shutil.which`` plus a fixed version."""

    VERSIONS = {"cmake": "3.28.3", "ctest": "3.28.3"}

    def lookup(self, name):
        import shutil

        if name in {"python3", "python"}:
            return HostRecord(name, sys.executable, "%d.%d.%d" % sys.version_info[:3])
        path = shutil.which(name)
        if path is None:
            return None
        # The PATH entry as found, never its realpath: multiplexing launchers
        # (rustup proxies, busybox, ccache) dispatch on the name they run as.
        return HostRecord(name, os.path.abspath(path), self.VERSIONS.get(name, ""))


def host_executable_guard(path: str) -> str:
    """``require_host_executable`` double: absolute regular file, else refuse."""
    candidate = Path(path)
    if not candidate.is_absolute() or not candidate.resolve().is_file():
        raise PermissionError("host executable rejected")
    return path


@dataclass(frozen=True)
class Window:
    text: str
    label: str
    bytes_read: int
    source_bytes: int
    truncated: bool


class RegistryOutputReader:
    """``JobOutputReader`` double over ``registry.stream`` (head + tail window)."""

    def __init__(self, registry):
        self._registry = registry

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        chunks, after, total = [], None, 0
        for _ in range(400):
            page = self._registry.stream(job_id, after=after, max_events=256, max_bytes=65_536)
            for event in page.events:
                chunks.append(event.data)
                total += len(event.data.encode("utf-8"))
            after = page.next_watermark
            if not page.has_more:
                break
        text = "".join(chunks)
        truncated = False
        if len(text.encode("utf-8")) > max_bytes:
            head = text[:head_bytes]
            text = head + text[-(max_bytes - len(head)):]
            truncated = True
        return Window(text, job_id, len(text.encode("utf-8")), total, truncated)


_PYTEST_SUMMARY = re.compile(r"^=*\s*(?P<body>(?:\d+ \w+(?:, )?)+) in [\d.]+s")


def summarize(text: str, label: str) -> dict:
    """Output-digest double: final line, pytest summary, FAILED/ERROR lines, tail."""
    lines = [line for line in text.splitlines() if line.strip()]
    final = lines[-1].strip() if lines else ""
    summary = None
    for line in reversed(lines[-200:]):
        match = _PYTEST_SUMMARY.match(line.strip())
        if match:
            counts = {}
            for part in match.group("body").split(", "):
                number, word = part.split(" ", 1)
                counts[word.rstrip("s") if word not in {"passed"} else word] = int(number)
            summary = {"line": line.strip().strip("= "), "passed": counts.get("passed", 0),
                       "failed": counts.get("failed", 0), "skipped": counts.get("skipped", 0),
                       "errors": counts.get("error", 0), "total": None,
                       "status": "failed" if counts.get("failed") or counts.get("error") else "passed"}
            break
    return {"source_label": label, "final_line": final, "summary": summary,
            "failure_lines": [line for line in lines if re.match(r"^(FAILED|ERROR) ", line)][:40],
            "tail": lines[-20:]}


class Stack:
    def __init__(self, tmp_path: Path):
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
        self.supervisor = ProcessTreeSupervisor()
        self.provider = SubprocessJobProvider(self.registry, process_cleanup=self.supervisor)
        report_root = self.state / "test-runs"
        self.planner = ProjectTestPlanner(HostLookup(), state_dir=str(self.state), redact=lambda t: t)
        self.launcher = ProcessTestLauncher(lambda: self.provider, lambda: self.registry,
                                            executable_guard=host_executable_guard,
                                            report_root=str(report_root))
        self.service = TestRunService(
            self.planner, self.launcher, ReportArtifactCollector(str(report_root)),
            output=RegistryOutputReader(self.registry), summarize=summarize,
            redact=lambda text: text, clock=time.time,
        )

    @staticmethod
    def context(**kwargs):
        return local_owner_context(correlation_id=uuid.uuid4().hex, **kwargs)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(allowed))
    built = Stack(tmp_path)
    built.allowed = allowed
    return built


def test_the_output_reader_double_keeps_head_and_tail(tmp_path):
    class Page:
        def __init__(self, events, has_more):
            self.events, self.has_more = events, has_more
            self.next_watermark = OutputWatermark(len(events))

    class Event:
        def __init__(self, data):
            self.data = data

    class Registry:
        def stream(self, job_id, *, after=None, max_events=256, max_bytes=65_536):
            return Page([Event("a" * 100), Event("b" * 100), Event("c" * 100)], False)

    window = RegistryOutputReader(Registry()).read_output("j", max_bytes=150, head_bytes=50)
    assert window.truncated and window.text.startswith("a" * 50) and window.text.endswith("c" * 100)
