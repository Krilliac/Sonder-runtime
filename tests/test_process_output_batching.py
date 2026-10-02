"""Batched process-output persistence (one registry transaction per window).

The provider coalesces the lines its pipe readers read into bounded batches
(``OutputBatchPolicy``: lines, bytes, delay) and publishes each batch through
``append_outputs``.  These tests pin the contract that batching must keep:
the same events (order, exact text, sequence numbers, retention) as the
one-commit-per-line path, bounded publication latency for a quiet child,
immediate flush on end of file and on cancellation, and a crash that loses at
most the unflushed window without duplicating or reordering what persisted.
"""
from __future__ import annotations

import os
import io
import random
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from sonder_runtime.adapters.debugging.launcher import _OutputCounter
from sonder_runtime.adapters.execution.output_batching import OutputBatcher, OutputBatchPolicy
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.execution.world_control import OutputStream, OutputWatermark
from sonder_runtime.application.jobs.durable_registry import (
    MAX_OUTPUT_APPEND_BATCH,
    DurableJobRegistry,
    OutputAppend,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.domain.common.errors import SonderError

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
STDOUT = OutputStream.STDOUT
STDERR = OutputStream.STDERR


def _identity(job_id: str) -> JobIdentity:
    return JobIdentity(job_id, "process", "execute", f"idem-{job_id}")


def _request(job_id: str, code: str, *args: str) -> ProcessJobRequest:
    return ProcessJobRequest(
        _identity(job_id), (sys.executable, "-c", code, *args), max_descendants=4,
        # Only a retained Windows Job Object can prove tree cleanup;
        # the taskkill fallback truthfully keeps cancellation pending.
        require_job_scope=os.name == "nt",
    )


def _events(registry, job_id: str):
    """Every retained event, in order, as (sequence, stream, data)."""
    out = []
    after = OutputWatermark(0)
    while True:
        page = registry.stream(job_id, after=after, max_events=256, max_bytes=1 << 20)
        out.extend((e.watermark.sequence, e.stream, e.data) for e in page.events)
        if not page.events or not page.has_more:
            return out
        after = page.next_watermark


def _last_sequence(registry, job_id: str) -> int:
    events = _events(registry, job_id)
    return events[-1][0] if events else 0


def _eventually(predicate, timeout: float, step: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


# -- throughput regression ------------------------------------------------------

# 20k lines through the real provider and SQLite registry.  Measured on Linux:
# ~0.25 s batched versus ~33 s with one commit per line (~1.65 ms/line); a
# hosted Windows runner commits in ~60 ms, i.e. ~20 commits (~1.5 s) batched
# versus ~20 minutes per line.  The bound leaves >5x headroom for a slow
# Windows runner and still fails a per-line implementation on any host.
THROUGHPUT_LINES = 20_000
THROUGHPUT_BOUND_SECONDS = 10.0


def test_twenty_thousand_lines_persist_within_a_bounded_time(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(registry, process_cleanup=ProcessTreeSupervisor())
    code = (
        "import sys\n"
        f"for i in range({THROUGHPUT_LINES}):\n"
        "    sys.stdout.write('%08d\\n' % i)\n"
    )
    started = time.monotonic()
    provider.start(_request("throughput", code))
    waited = provider.wait("throughput", timeout=120)
    assert waited.record.status is JobStatus.SUCCEEDED
    complete = _eventually(
        lambda: _last_sequence(registry, "throughput") >= THROUGHPUT_LINES,
        max(0.0, started + THROUGHPUT_BOUND_SECONDS - time.monotonic()),
        step=0.05,
    )
    elapsed = time.monotonic() - started
    assert complete and elapsed < THROUGHPUT_BOUND_SECONDS, (
        f"{THROUGHPUT_LINES} lines took {elapsed:.1f}s to persist"
    )
    # Exact content of the retained tail: one event per line, contiguous
    # sequences, sequence n carries line n-1.
    events = _events(registry, "throughput")
    assert events[-1][0] == THROUGHPUT_LINES
    assert [seq for seq, _, _ in events] == list(range(events[0][0], THROUGHPUT_LINES + 1))
    assert all(data == "%08d\n" % (seq - 1) and stream is STDOUT for seq, stream, data in events)
    assert registry.stream("throughput").truncated is True


# -- byte-exact equivalence with the per-line path -----------------------------

_TRICKY_CHILD = textwrap.dedent(r'''
    import os, sys
    out = sys.stdout.buffer
    out.write(b"plain\n")
    out.write(b"crlf\r\n")
    out.write(b"lone\rcr\n")
    out.write(b"blank\n\n\n")
    out.write("unicode é中\U0001f600\n".encode("utf-8"))
    out.write(b"x" * 20000 + b"\n")
    out.flush()
    os.write(2, b"to stderr\n")
    out.write(b"partial last line")
    out.flush()
''')

_INVALID_UTF8_CHILD = textwrap.dedent(r'''
    import sys, time
    out = sys.stdout.buffer
    out.write(b"good one\n")
    out.flush()
    time.sleep(0.3)
    out.write(b"bad \xff\xfe bytes\n")
    out.flush()
    time.sleep(0.3)
    out.write(b"after bad\n")
    out.flush()
''')


def _run_with_policy(tmp_path, name, code, policy):
    registry = SQLiteDurableJobRegistry(tmp_path / f"{name}.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(), output_batch=policy,
    )
    provider.start(_request(name, code))
    waited = provider.wait(name, timeout=60)
    return waited, _events(registry, name)


@pytest.mark.parametrize("code", [_TRICKY_CHILD, _INVALID_UTF8_CHILD], ids=["tricky", "invalid-utf8"])
def test_batched_output_is_identical_to_the_per_line_path(tmp_path, code):
    per_line = OutputBatchPolicy(max_lines=1)
    wide = OutputBatchPolicy(max_lines=1024, max_bytes=1 << 20, max_delay_seconds=30)
    waited_one, one = _run_with_policy(tmp_path, "one", code, per_line)
    waited_many, many = _run_with_policy(tmp_path, "many", code, wide)
    assert waited_one.record.status is waited_many.record.status is JobStatus.SUCCEEDED
    # stdout and stderr are separate pipes; compare each stream's sequence of
    # lines, and the combined sequence numbers are contiguous either way.
    for stream in (STDOUT, STDERR):
        assert [d for _, s, d in one if s is stream] == [d for _, s, d in many if s is stream]
    assert [seq for seq, _, _ in many] == list(range(1, len(many) + 1))


def test_line_boundaries_and_partial_last_line_survive_batching(tmp_path):
    waited, events = _run_with_policy(
        tmp_path, "tricky", _TRICKY_CHILD,
        OutputBatchPolicy(max_lines=1024, max_bytes=1 << 20, max_delay_seconds=30),
    )
    assert waited.record.status is JobStatus.SUCCEEDED
    stdout = [d for _, s, d in events if s is STDOUT]
    # Text-mode pipes use universal newlines: CRLF and a lone CR end a line.
    assert stdout[:7] == [
        "plain\n", "crlf\n", "lone\n", "cr\n", "blank\n", "\n", "\n",
    ]
    assert stdout[7] == "unicode é中\U0001f600\n"
    # A line longer than the inline bound is kept inline up to that bound.
    assert stdout[8] == "x" * (16 * 1024)
    assert stdout[-1] == "partial last line"
    assert [d for _, s, d in events if s is STDERR] == ["to stderr\n"]


def test_invalid_utf8_keeps_todays_behaviour(tmp_path):
    waited, events = _run_with_policy(
        tmp_path, "bad", _INVALID_UTF8_CHILD,
        OutputBatchPolicy(max_lines=1024, max_bytes=1 << 20, max_delay_seconds=30),
    )
    # The text-mode reader stops at the undecodable line (a ValueError ends
    # output quietly); the job still succeeds and earlier lines persist.
    assert waited.record.status is JobStatus.SUCCEEDED
    assert [d for _, _, d in events] == ["good one\n"]


# -- registry batch API ---------------------------------------------------------

def _registries(tmp_path, bounds):
    return (
        ("memory", DurableJobRegistry(output_bounds=bounds)),
        ("sqlite", SQLiteDurableJobRegistry(tmp_path / f"reg-{bounds[0]}-{bounds[1]}.db",
                                            output_bounds=bounds)),
    )


def _state(registry, job_id):
    page = registry.stream(job_id, max_events=10_000, max_bytes=1 << 30)
    return (
        [(e.watermark.sequence, e.stream, e.data, e.spill) for e in page.events],
        page.truncated,
    )


# Bounds chosen so trimming happens by event count, by bytes, and for events
# larger than the whole byte budget.  Line counts stay small: the per-line
# reference commits once per line, ~60 ms each on a hosted Windows runner.
@pytest.mark.parametrize("bounds", [(20, 300), (5, 64), (3, 10_000), (1000, 7)])
def test_batch_append_leaves_the_registry_exactly_as_line_at_a_time(tmp_path, bounds):
    rng = random.Random(f"{bounds}")
    lines = [
        OutputAppend(rng.choice((STDOUT, STDERR)), "é" * rng.randrange(0, 5) + "y" * rng.randrange(0, 40))
        for _ in range(80)
    ]
    for kind, registry in _registries(tmp_path, bounds):
        registry.start(_identity("single"))
        registry.start(_identity("batched"))
        offset = 0
        while offset < len(lines):
            size = rng.choice((1, 2, 7, 30))
            chunk = lines[offset:offset + size]
            for entry in chunk:
                registry.append_output("single", entry.stream, entry.data)
            registry.append_outputs("batched", chunk)
            offset += len(chunk)
            # Identical at every batch boundary: retained events, sequence
            # numbers, and the truncation (dropped-before) watermark.
            assert _state(registry, "batched") == _state(registry, "single"), (kind, offset)
        page = registry.stream("batched", after=OutputWatermark(len(lines) - 1))
        assert [e.watermark.sequence for e in page.events] in ([len(lines)], [])


def test_batch_append_is_all_or_nothing_and_validated_like_one_line(tmp_path):
    for kind, registry in _registries(tmp_path, (256, 64 * 1024)):
        registry.start(_identity("job"))
        registry.append_outputs("job", [OutputAppend(STDOUT, "a\n")])
        with pytest.raises(TypeError, match="OutputAppend values"):
            registry.append_outputs("job", [OutputAppend(STDOUT, "b\n"), ("stdout", "c\n")])
        with pytest.raises(TypeError, match="stream and data"):
            OutputAppend("stdout", "x")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="stream and data"):
            OutputAppend(STDOUT, b"x")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="exceeds"):
            registry.append_outputs("job", [OutputAppend(STDOUT, "z")] * (MAX_OUTPUT_APPEND_BATCH + 1))
        with pytest.raises(KeyError):
            registry.append_outputs("missing", [OutputAppend(STDOUT, "x")])
        with pytest.raises(KeyError):
            registry.append_outputs("missing", [])
        registry.append_outputs("job", [])
        assert [data for _, _, data, _ in _state(registry, "job")[0]] == ["a\n"], kind
        registry.append_outputs("job", [OutputAppend(STDERR, "b\n"), OutputAppend(STDOUT, "c")])
        assert [(seq, stream, data) for seq, stream, data, _ in _state(registry, "job")[0]] == [
            (1, STDOUT, "a\n"), (2, STDERR, "b\n"), (3, STDOUT, "c"),
        ], kind


def test_sqlite_batch_is_one_transaction(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    registry.start(_identity("job"))
    import sqlite3

    real_connect = registry._connect_factory
    opened = []

    def counting(*args, **kwargs):
        opened.append(1)
        return real_connect(*args, **kwargs)

    registry._connect_factory = counting
    registry.append_outputs("job", [OutputAppend(STDOUT, f"{i}\n") for i in range(1000)])
    assert len(opened) == 1
    assert _last_sequence(registry, "job") == 1000
    # A failing statement mid-batch rolls the whole batch back.

    class _Failing(sqlite3.Connection):
        def executemany(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

    registry._connect_factory = lambda path, **kw: sqlite3.connect(path, factory=_Failing, **kw)
    with pytest.raises(SonderError):
        registry.append_outputs("job", [OutputAppend(STDOUT, "lost\n")] * 3)
    registry._connect_factory = real_connect
    registry.append_outputs("job", [OutputAppend(STDOUT, "next\n")])
    events = _events(registry, "job")
    assert events[-1] == (1001, STDOUT, "next\n")
    assert all(data != "lost\n" for _, _, data in events)


# -- batch boundaries (OutputBatcher) --------------------------------------------

class _Sink:
    def __init__(self, gate: threading.Event | None = None):
        self.batches: list[tuple[tuple[OutputStream, str], ...]] = []
        self.times: list[float] = []
        self.gate = gate
        self.lock = threading.Lock()

    def __call__(self, batch):
        if self.gate is not None:
            self.gate.wait(10)
        with self.lock:
            self.batches.append(tuple(batch))
            self.times.append(time.monotonic())

    def lines(self):
        with self.lock:
            return [data for batch in self.batches for _, data in batch]


def _running(batcher):
    thread = threading.Thread(target=batcher.run, daemon=True)
    thread.start()
    return thread


def test_batch_closes_on_the_line_bound():
    sink = _Sink()
    batcher = OutputBatcher(sink, writers=1, policy=OutputBatchPolicy(3, 1 << 20, 30))
    thread = _running(batcher)
    for i in range(7):
        assert batcher.put(STDOUT, f"{i}\n")
    assert _eventually(lambda: len(sink.lines()) == 6, 5)
    time.sleep(0.2)
    assert len(sink.lines()) == 6  # the seventh line waits for its window
    batcher.writer_done()
    thread.join(5)
    assert not thread.is_alive()
    assert [len(batch) for batch in sink.batches] == [3, 3, 1]
    assert sink.lines() == [f"{i}\n" for i in range(7)]


def test_batch_closes_on_the_byte_bound_and_bounds_the_unpersisted_window():
    gate = threading.Event()
    sink = _Sink(gate)
    policy = OutputBatchPolicy(max_lines=1000, max_bytes=10, max_delay_seconds=30)
    batcher = OutputBatcher(sink, writers=1, policy=policy)
    thread = _running(batcher)
    produced = []

    def produce():
        for i in range(12):
            batcher.put(STDOUT, "abc\n")
            produced.append(i)
        batcher.writer_done()

    producer = threading.Thread(target=produce, daemon=True)
    producer.start()
    time.sleep(0.3)
    # The persister is stuck committing: readers are held once the window
    # (in flight plus pending) reaches the byte bound; 3 lines = 12 bytes.
    assert len(produced) == 3
    gate.set()
    producer.join(5)
    thread.join(5)
    assert sink.lines() == ["abc\n"] * 12
    assert all(sum(len(d) for _, d in batch) < policy.max_bytes + 4 for batch in sink.batches)


def test_a_quiet_writer_is_flushed_on_the_time_bound():
    sink = _Sink()
    batcher = OutputBatcher(sink, writers=1, policy=OutputBatchPolicy(1000, 1 << 20, 0.2))
    thread = _running(batcher)
    put_at = time.monotonic()
    batcher.put(STDOUT, "only\n")
    assert _eventually(lambda: sink.lines() == ["only\n"], 5)
    latency = sink.times[0] - put_at
    assert 0.15 <= latency < 2.0, latency
    batcher.writer_done()
    thread.join(5)
    assert not thread.is_alive()


def test_end_of_file_flushes_immediately_without_waiting_for_the_window():
    sink = _Sink()
    batcher = OutputBatcher(sink, writers=2, policy=OutputBatchPolicy(1000, 1 << 20, 60))
    thread = _running(batcher)
    batcher.put(STDOUT, "a\n")
    batcher.put(STDERR, "b")
    batcher.writer_done()
    time.sleep(0.2)
    assert sink.lines() == []  # one reader is still open and the window is 60 s
    closed = time.monotonic()
    batcher.writer_done()
    thread.join(5)
    assert not thread.is_alive()
    assert sink.batches == [((STDOUT, "a\n"), (STDERR, "b"))]
    assert sink.times[0] - closed < 2.0


def test_flush_publishes_pending_lines_and_reports_durability():
    sink = _Sink()
    batcher = OutputBatcher(sink, writers=1, policy=OutputBatchPolicy(1000, 1 << 20, 60))
    thread = _running(batcher)
    batcher.put(STDOUT, "x\n")
    assert batcher.flush(5) is True
    assert sink.lines() == ["x\n"]
    assert batcher.flush(5) is True  # nothing pending is trivially durable
    batcher.writer_done()
    thread.join(5)
    assert batcher.flush(1) is True  # finished cleanly


def test_a_persistence_failure_stops_readers_and_is_reported():
    failures = []

    def persist(batch):
        raise RuntimeError("store full")

    batcher = OutputBatcher(persist, writers=1, policy=OutputBatchPolicy(1, 1 << 20, 60),
                            on_failure=failures.append)
    thread = _running(batcher)
    assert batcher.put(STDOUT, "a\n") is True
    assert _eventually(lambda: batcher.finished, 5)
    assert batcher.put(STDOUT, "b\n") is False
    assert batcher.flush(1) is False
    assert [type(exc) for exc in failures] == [RuntimeError]
    thread.join(5)


@pytest.mark.parametrize("policy", [
    dict(max_lines=0), dict(max_lines=MAX_OUTPUT_APPEND_BATCH + 1), dict(max_lines=True),
    dict(max_bytes=0), dict(max_delay_seconds=0), dict(max_delay_seconds=61),
])
def test_policy_bounds_are_validated(policy):
    with pytest.raises(ValueError):
        OutputBatchPolicy(**policy)


def test_provider_rejects_a_non_policy():
    with pytest.raises(TypeError, match="OutputBatchPolicy"):
        SubprocessJobProvider(DurableJobRegistry(), process_cleanup=ProcessTreeSupervisor(),
                              output_batch={"max_lines": 1})


# -- provider: latency, cancellation, limit accounting -------------------------

def test_quiet_child_output_is_visible_within_the_time_bound(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        output_batch=OutputBatchPolicy(1000, 1 << 20, 0.1),
    )
    release = tmp_path / "release"
    code = (
        "import os,sys,time\n"
        "print('early', flush=True)\n"
        "deadline = time.monotonic() + 30\n"
        "while not os.path.exists(sys.argv[1]) and time.monotonic() < deadline:\n"
        "    time.sleep(.01)\n"
    )
    provider.start(_request("quiet", code, str(release)))
    try:
        assert _eventually(lambda: _events(registry, "quiet"), 5)
        assert [d for _, _, d in _events(registry, "quiet")] == ["early\n"]
        assert registry.poll("quiet").is_terminal is False
    finally:
        release.write_text("go", encoding="utf-8")
    assert provider.wait("quiet", timeout=30).record.status is JobStatus.SUCCEEDED


def test_cancel_mid_batch_is_prompt_and_publishes_the_pending_window(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        # A 30 s window: without the cancel flush these lines would sit unpublished.
        output_batch=OutputBatchPolicy(1000, 1 << 20, 30),
    )
    ready = tmp_path / "ready"
    code = (
        "import sys,time,pathlib\n"
        "for i in range(3): print('line %d' % i, flush=True)\n"
        "pathlib.Path(sys.argv[1]).write_text('x')\n"
        "time.sleep(120)\n"
    )
    started = provider.start(_request("cancel-mid", code, str(ready)))
    assert _eventually(ready.exists, 20)
    time.sleep(0.3)
    assert _events(registry, "cancel-mid") == []  # the lines are pending in the window
    began = time.monotonic()
    provider.cancel("cancel-mid", "operator cancel")
    assert time.monotonic() - began < 10
    # The root may not be reaped by the time cancel returns; the provider's
    # bounded retry then finishes the cleanup.
    assert _eventually(lambda: registry.poll("cancel-mid").status is JobStatus.CANCELLED, 15)
    assert _eventually(lambda: len(_events(registry, "cancel-mid")) == 3, 5)
    assert time.monotonic() - began < 15  # far inside the 30 s window
    assert [d for _, _, d in _events(registry, "cancel-mid")] == [
        "line 0\n", "line 1\n", "line 2\n",
    ]
    from sonder_runtime.adapters.process_liveness import PROCESS_DEAD, probe_process

    assert _eventually(lambda: probe_process(started.process_id)[0] == PROCESS_DEAD, 15)


def test_output_limit_accounting_counts_the_same_bytes_for_batches(tmp_path):
    """The launcher's counter reads registry events; batches must not change the count."""
    line = "L" * 99 + "\n"
    limit = 150_000  # crossed in the middle of the third 700-line batch
    counts = {}
    # The per-line reference uses the in-memory registry (thousands of
    # single-line SQLite commits would dominate a slow runner); registry
    # equivalence between the two paths is proven by the test above.
    for label, batch, registry in (
        ("per-line", 1, DurableJobRegistry()),
        ("batched-memory", 700, DurableJobRegistry()),
        ("batched-sqlite", 700, SQLiteDurableJobRegistry(tmp_path / "flood.db")),
    ):
        registry.start(_identity("flood"))
        counter = _OutputCounter()
        tripped_at = None
        written = 0
        while written < 2800:
            registry.append_outputs("flood", [OutputAppend(STDOUT, line)] * batch)
            written += batch
            if counter.update(registry, "flood") > limit and tripped_at is None:
                tripped_at = written
        counts[label] = (counter.bytes, tripped_at)
        # Counted bytes equal what was written, even though the registry kept
        # only its 64 KiB tail and most events were dropped between polls.
        assert counter.bytes == written * len(line)
    # Per line, the limit trips at the first line past it; batched, at the
    # end of the batch containing that line.  Same threshold, same bytes.
    assert counts["per-line"][1] == limit // len(line) + 1
    assert counts["batched-memory"] == counts["batched-sqlite"] == (2800 * len(line), 2100)
    assert counts["per-line"][0] == 2800 * len(line)


# -- crash and restart ------------------------------------------------------------

_HOST = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from sonder_runtime.adapters.execution.output_batching import OutputBatchPolicy
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
    from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
    from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus

    registry = SQLiteDurableJobRegistry(sys.argv[2])
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        output_batch=OutputBatchPolicy(max_lines=50, max_bytes=1 << 20, max_delay_seconds=60),
    )
    child = (
        "import sys, time\\n"
        "for i in range(120):\\n"
        "    sys.stdout.write('line %04d\\\\n' % i)\\n"
        "sys.stdout.flush()\\n"
        "time.sleep(120)\\n"
    )
    provider.start(ProcessJobRequest(
        JobIdentity("crash-job", "process", "execute", "idem-crash"),
        (sys.executable, "-c", child), max_descendants=4,
    ))
    # The job's owner marks it running, as a job worker would.
    registry.transition("crash-job", JobStatus.RUNNING)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        page = registry.stream("crash-job", max_events=256, max_bytes=1 << 20)
        if page.events and page.events[-1].watermark.sequence >= 100:
            break
        time.sleep(0.02)
    print("ready", flush=True)
    time.sleep(120)
''')


def test_a_crash_loses_at_most_the_unflushed_window_without_duplicates(tmp_path):
    from sonder_runtime.adapters.process_liveness import PROCESS_DEAD, probe_process

    db = tmp_path / "jobs.db"
    host = subprocess.Popen(
        [sys.executable, "-c", _HOST, str(REPO), str(db)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert host.stdout.readline().strip() == "ready", host.stderr.read()
        # 120 lines were printed: two 50-line batches are durable, the last
        # 20 lines sit in the unflushed 60 s window.  Crash the host now.
        time.sleep(0.2)
    finally:
        host.kill()
        host.wait(10)
    # Restart: a fresh registry over the same database.
    registry = SQLiteDurableJobRegistry(db)
    child_pid = registry.view("crash-job").process_id
    try:
        events = _events(registry, "crash-job")
        # An exact, gap-free, in-order prefix of what the child printed.
        assert [seq for seq, _, _ in events] == list(range(1, 101))
        assert [data for _, _, data in events] == ["line %04d\n" % i for i in range(100)]
        lost = 120 - len(events)
        assert 0 < lost <= 50  # at most the unflushed window (max_lines)
        assert registry.poll("crash-job").status is JobStatus.RUNNING
    finally:
        report = registry.reconcile_with_cleanup(
            ProcessTreeSupervisor(), owner_instance_id="crashed-host", owner_alive=False,
        )
    assert report.cleanup_receipts and report.cleanup_receipts[0].requested is True
    if os.name != "nt":
        # (Windows taskkill cannot prove the tree clean after a crash; see
        # test_api003_restart_recovery.)  POSIX proves it and interrupts.
        assert report.interrupted_job_ids == ("crash-job",)
        assert registry.poll("crash-job").status is JobStatus.INTERRUPTED
        assert _eventually(lambda: probe_process(child_pid)[0] == PROCESS_DEAD, 15)
    # Recovery neither replays nor reorders output, and the watermark keeps
    # counting from the durable prefix.
    assert _events(registry, "crash-job") == events
    registry.append_outputs("crash-job", [OutputAppend(STDERR, "after restart\n")])
    assert _events(registry, "crash-job")[-1] == (101, STDERR, "after restart\n")
    page = registry.stream("crash-job", after=OutputWatermark(100))
    assert [e.data for e in page.events] == ["after restart\n"]


# -- review follow-ups: drain timeout, cancel flush, launch-failure cleanup -----

class _GatedRegistry(SQLiteDurableJobRegistry):
    """SQLite registry whose batch appends block until ``gate`` is set."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = threading.Event()
        self.entered = threading.Event()

    def append_outputs(self, job_id, entries):
        entries = tuple(entries)
        self.entered.set()
        assert self.gate.wait(60), "test gate never opened"
        return super().append_outputs(job_id, entries)


def test_a_drain_that_times_out_fails_the_job_closed(tmp_path, monkeypatch):
    from sonder_runtime.adapters.execution import process_jobs

    monkeypatch.setattr(process_jobs, "OUTPUT_DRAIN_SECONDS", 0.5)
    registry = _GatedRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(registry, process_cleanup=ProcessTreeSupervisor())
    try:
        provider.start(_request("slow-store", "print('only line', flush=True)"))
        waited = provider.wait("slow-store", timeout=30)
        # The line was read but is not durable: the job must not read as a
        # success with its output missing.
        assert waited.record.status is JobStatus.FAILED, waited.record
        assert waited.record.error == "process output persistence failed (TimeoutError)"
        assert waited.exit_code == 0
        assert _events(registry, "slow-store") == []
    finally:
        registry.gate.set()
    # The late commit still lands; nothing is lost or duplicated.
    assert _eventually(lambda: [d for _, _, d in _events(registry, "slow-store")] == [
        "only line\n",
    ], 10)


def test_newline_free_output_is_read_with_a_bound_and_fails_closed(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        output_batch=OutputBatchPolicy(max_bytes=1024, max_line_bytes=1024),
    )
    provider.start(_request("long-line", "import sys; sys.stdout.write('x' * 4096)"))
    waited = provider.wait("long-line", timeout=30)
    assert waited.record.status is JobStatus.FAILED
    assert "output" in waited.record.error


@pytest.mark.parametrize("data", ["x" * 4096, "😀" * 1024])
def test_pipe_reader_never_requests_an_unbounded_line(data):
    class Pipe(io.StringIO):
        limits = []

        def readline(self, size=-1):
            self.limits.append(size)
            assert 0 < size <= 1025
            return super().readline(size)

    class Batcher:
        policy = OutputBatchPolicy(max_line_bytes=1024)
        data = []

        def put(self, stream, value):
            self.data.append(value)
            return True

        def writer_done(self):
            pass

    provider = SubprocessJobProvider(
        DurableJobRegistry(), process_cleanup=ProcessTreeSupervisor(),
    )
    pipe = Pipe(data)
    batcher = Batcher()
    provider._read_output("unbounded-line", STDOUT, pipe, batcher)
    assert pipe.limits and batcher.data == []
    assert provider._output_failures["unbounded-line"] == "ValueError"


def test_spill_limit_failure_cannot_be_reported_as_success(tmp_path):
    from sonder_runtime.adapters.execution.durable_output import DurableExecutionOutput, SQLiteSpillStore

    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        output=DurableExecutionOutput(SQLiteSpillStore(tmp_path / "spills.db"), max_bytes=32),
        inline_output_bytes=8,
    )
    provider.start(_request("oversized-spill", "print('x' * 100)"))
    waited = provider.wait("oversized-spill", timeout=30)
    assert waited.exit_code == 0
    assert waited.record.status is JobStatus.FAILED
    assert waited.record.error == "process output persistence failed (ValueError)"


def test_pruned_output_references_release_spill_quota(tmp_path):
    from sonder_runtime.adapters.execution.durable_output import DurableExecutionOutput, SQLiteSpillStore

    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", output_bounds=(1, 128))
    output = DurableExecutionOutput(
        SQLiteSpillStore(tmp_path / "spills.db", max_owner_bytes=42, max_total_bytes=42),
    )
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(), output=output,
        inline_output_bytes=8, output_batch=OutputBatchPolicy(max_lines=1),
    )
    provider.start(_request("rolling-spills", "for i in range(3): print(str(i) * 20, flush=True)"))
    waited = provider.wait("rolling-spills", timeout=30)
    assert waited.record.status is JobStatus.SUCCEEDED
    page = registry.stream("rolling-spills")
    assert len(page.events) == 1
    assert output.read(page.events[0].spill, max_bytes=64) == b"2" * 20 + b"\n"


def test_cancel_requests_publication_before_the_kill_and_never_waits_for_storage(tmp_path):
    registry = _GatedRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        registry, process_cleanup=ProcessTreeSupervisor(),
        # A 30 s window: only an explicit flush request publishes these lines
        # before the kill closes the pipes.
        output_batch=OutputBatchPolicy(1000, 1 << 20, 30),
    )
    ready = tmp_path / "ready"
    code = (
        "import sys,time,pathlib\n"
        "for i in range(3): print('line %d' % i, flush=True)\n"
        "pathlib.Path(sys.argv[1]).write_text('x')\n"
        "time.sleep(120)\n"
    )
    started = provider.start(_request("gated-cancel", code, str(ready)))
    observed = []
    real_quiesce = provider._quiesce_containment

    def observing_quiesce(job_id, *, force):
        # The kill happens here (or right after, through the cleanup
        # contract).  By now cancel must already have asked the persister to
        # publish the pending window: it is committing (and blocked on the
        # gated store) rather than still waiting out its 30 s window.
        observed.append(registry.entered.wait(5))
        return real_quiesce(job_id, force=force)

    provider._quiesce_containment = observing_quiesce
    try:
        assert _eventually(ready.exists, 20)
        time.sleep(0.3)
        assert not registry.entered.is_set()  # pending in the window
        began = time.monotonic()
        provider.cancel("gated-cancel", "operator cancel")
        # The persister is blocked in the store; cancel must not wait for it.
        assert time.monotonic() - began < 8
        assert observed and observed[0] is True
    finally:
        registry.gate.set()
    assert _eventually(lambda: registry.poll("gated-cancel").status is JobStatus.CANCELLED, 15)
    assert _eventually(lambda: [d for _, _, d in _events(registry, "gated-cancel")] == [
        "line 0\n", "line 1\n", "line 2\n",
    ], 10)
    from sonder_runtime.adapters.process_liveness import PROCESS_DEAD, probe_process

    assert _eventually(lambda: probe_process(started.process_id)[0] == PROCESS_DEAD, 15)


def test_a_reader_that_fails_to_start_leaves_no_output_bookkeeping(tmp_path, monkeypatch):
    from sonder_runtime.adapters.execution import process_jobs

    real_thread = process_jobs.owned_runtime_thread
    batchers = []

    class _Unstartable:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("cannot start thread")

    def thread_factory(*args, name="", **kwargs):
        if name.endswith("-stderr"):
            return _Unstartable()
        if name.endswith("-persist"):
            batchers.append(kwargs["target"].__self__)
        return real_thread(*args, name=name, **kwargs)

    monkeypatch.setattr(process_jobs, "owned_runtime_thread", thread_factory)
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(registry, process_cleanup=ProcessTreeSupervisor())
    code = "import time\nprint('hi', flush=True)\ntime.sleep(120)\n"
    with pytest.raises(RuntimeError, match="cannot start thread"):
        provider.start(_request("no-reader", code))
    record = registry.poll("no-reader")
    assert record.status is JobStatus.FAILED, record
    assert "no-reader" not in provider._output_batchers
    assert "no-reader" not in provider._output_threads
    assert "no-reader" not in provider._processes
    # The started reader reaches EOF once the child is killed, and the
    # never-started one was released, so the persister finishes.
    assert len(batchers) == 1
    assert _eventually(lambda: batchers[0].finished, 15)
