from __future__ import annotations

import sys
import time

import pytest

from sonder_runtime.adapters.execution.persistent_terminal import (
    PersistentTerminalError,
    SQLitePersistentTerminalService,
    TerminalCleanupError,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.world_control import OutputWatermark
from sonder_runtime.application.ports.execution_world import TerminalRequest


def _context():
    return local_owner_context(correlation_id="exec003-test")


def _request(code: str) -> TerminalRequest:
    return TerminalRequest((sys.executable, "-u", "-c", code))


def _wait_for_page(service, terminal_id, *, after=None, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = service.read_page(terminal_id, after=after, max_events=16, max_bytes=4096)
        if page.events or not page.has_more:
            if page.events:
                return page
        time.sleep(0.01)
    raise AssertionError("terminal output did not arrive")


def _wait_for_sequence(service, terminal_id, sequence, *, timeout=3.0):
    """Block until the durable log has advanced to ``sequence`` or beyond."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = service.read_page(
            terminal_id, after=OutputWatermark(0), max_events=16, max_bytes=4096
        )
        if page.next_watermark.sequence >= sequence:
            return page
        time.sleep(0.01)
    raise AssertionError(f"terminal output did not reach sequence {sequence}")


def test_persistent_terminal_reconnects_and_replays_durable_watermarks(tmp_path):
    code = "import sys; [print('echo:'+line.strip(), flush=True) for line in sys.stdin]"
    service = SQLitePersistentTerminalService(tmp_path / "term.sqlite", world_id="world-1")
    first = service.open_named("term-1", _request(code), _context())
    first.send("ready\n")

    page = _wait_for_page(service, "term-1")
    assert page.events[0].stream.value == "stdout"
    assert "echo:ready" in page.events[0].data
    watermark = page.next_watermark

    second = service.reconnect("term-1")
    second.resize(columns=120, rows=40)
    resumed = service.read_page("term-1", after=watermark)
    assert all(event.watermark.sequence > watermark.sequence for event in resumed.events)

    assert service.stop("term-1", timeout=2.0) is True
    reopened = SQLitePersistentTerminalService(tmp_path / "term.sqlite", world_id="world-1")
    durable = reopened.read_page("term-1", after=OutputWatermark(0))
    assert durable.events[0].watermark == page.events[0].watermark
    with pytest.raises(PersistentTerminalError, match="stopped"):
        reopened.reconnect("term-1")


def test_reads_are_bounded_and_report_retention_gap(tmp_path):
    # The child echoes a line only after the test sends it, and the next line
    # is sent only once the previous echo is durable, so the three outputs
    # land as at least three separate journal events whatever the reader
    # thread's timing. A timed burst from the child left that to scheduler
    # luck: on a loaded runner the reader coalesced two prints into one
    # chunk, nothing was evicted, and the gap was never reported.
    code = "import sys; [print('echo:'+line.strip(), flush=True) for line in sys.stdin]"
    service = SQLitePersistentTerminalService(
        tmp_path / "term.sqlite", world_id="world-1", max_events=2, max_bytes=64
    )
    handle = service.open_named("term-1", _request(code), _context())
    for sequence, value in enumerate(("one", "two", "three"), start=1):
        handle.send(value + "\n")
        _wait_for_sequence(service, "term-1", sequence)
    page = service.read_page("term-1", after=OutputWatermark(0), max_events=16, max_bytes=4096)
    assert len(page.events) <= 2
    assert page.truncated is True
    bounded = service.read_page("term-1", after=OutputWatermark(0), max_events=1, max_bytes=4)
    assert len(bounded.events) == 1
    assert bounded.has_more is True
    with pytest.raises(ValueError, match="max_events"):
        service.read_page("term-1", max_events=0)
    service.stop("term-1", timeout=2.0)


def test_cleanup_does_not_claim_quiescence_before_process_exit(tmp_path):
    code = "import time; time.sleep(10)"
    service = SQLitePersistentTerminalService(tmp_path / "term.sqlite", world_id="world-1")
    service.open_named("term-1", _request(code), _context())
    pending = service.cleanup(timeout=0)
    assert pending.quiescent is False
    assert pending.active_resources == 1
    complete = service.cleanup(timeout=2.0)
    assert complete.quiescent is True
    assert complete.active_resources == 0


def test_reconnect_fails_closed_when_durable_owner_is_gone(tmp_path):
    code = "import time; time.sleep(10)"
    path = tmp_path / "term.sqlite"
    owner = SQLitePersistentTerminalService(path, world_id="world-1")
    owner.open_named("term-1", _request(code), _context())
    orphan = SQLitePersistentTerminalService(path, world_id="world-1")
    with pytest.raises(TerminalCleanupError, match="no live owner"):
        orphan.reconnect("term-1")
    assert owner.cleanup(timeout=2.0).quiescent is True
