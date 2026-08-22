"""The end report says what standing the claim in it was made under.

A report that opens with ``result: complete`` and nothing else asks the reader
to supply the missing half themselves. ``calibration.should_verify`` already
answers "does the measured record demand a citation before a claim is
believed"; until now that answer only reached the caller when a run *lacked*
verification (``finish_final``, tests/test_agent_verification_gate.py). The
standing itself -- the counts and the verdict -- belongs in the report header,
on every run, next to the result it qualifies.

Two things this must not do:

* **Repeat what ``finish_final`` already prints.** That prefix is a claim about
  *this run* ("claimed completion without verifiers"), quoting
  ``should_verify``'s reason sentence verbatim. The header line is a different
  fact -- the standing -- so it carries the *measurement and the verdict* and
  never the reason prose. Printing the same sentence twice would make the
  report look like two findings where there is one.
* **Render a thin sample as a rate.** Below ``MIN_SAMPLE`` there is no rate;
  ``0.0%`` there would say "nothing delegated has ever been good", which is a
  different and false statement.

``activity_tracker`` is deliberately dependency-free and holds no database
connection, so it cannot measure anything itself: the line is composed by the
server and passed in, and the default output stays byte-identical.
"""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import calibration
import server


UNMEASURED_RECORD: dict = {}
POOR_RECORD = {"accepted": 40, "rejected": 60}
GOOD_RECORD = {"accepted": 95, "rejected": 5}


class _FakeConn:
    def close(self):
        pass


def _record(monkeypatch, counts):
    monkeypatch.setattr(server, "_open_db_readonly", lambda: _FakeConn())
    # Signature-agnostic on purpose. `_counts` gained a provenance filter with
    # #62, and a double pinning an argument list that no assertion here
    # concerns is the trap this repo has been bitten by before: it breaks on a
    # parameter these tests do not care about. They supply the counts and
    # assert on the rendered standing, nothing more.
    monkeypatch.setattr(calibration, "_counts", lambda *a, **k: dict(counts))


def _standing_line(text):
    lines = [ln for ln in text.splitlines() if ln.startswith("standing:")]
    assert lines, "the end report should carry a standing line:\n%s" % text
    assert len(lines) == 1, "one standing line, not several: %r" % (lines,)
    return lines[0]


# --- activity_tracker stays dependency-free -------------------------------


def test_the_end_report_is_byte_identical_when_no_standing_is_supplied():
    """The tracker measures nothing; an uncomposed report is unchanged."""
    response = {
        "status": "complete", "elapsed_ms": 5, "model_calls": 1, "tool_calls": 2,
    }

    assert activity_tracker.format_end_report(response) == "\n".join([
        "=== END REPORT ===",
        "result: complete",
        "elapsed: 5ms | model calls: 1 | tool calls: 2",
        "files: +0 ~0 -0 | lines: +0 ~0 -0",
    ])


def test_a_supplied_standing_rides_in_the_header_beside_the_result():
    response = {"status": "complete"}

    report = activity_tracker.format_end_report(
        response, calibration_line="standing: verify before claiming done: yes"
    )

    lines = report.splitlines()
    assert lines[1] == "result: complete"
    assert lines[2] == "standing: verify before claiming done: yes"


# --- the server composes it from the measured record ----------------------


@pytest.mark.parametrize(
    "counts,expected_verdict",
    [(POOR_RECORD, "yes"), (GOOD_RECORD, "no")],
)
def test_the_standing_line_carries_should_verifys_verdict(
    monkeypatch, counts, expected_verdict
):
    _record(monkeypatch, counts)
    demanded, _reason = calibration.should_verify(_FakeConn(), "caller")
    assert demanded is (expected_verdict == "yes")

    line = _standing_line(server._agent_end_report_standing_line())

    assert "verify before claiming done: %s" % expected_verdict in line
    assert "caller-judged" in line


def test_the_standing_line_reports_the_counts_not_a_mood(monkeypatch):
    _record(monkeypatch, POOR_RECORD)

    line = _standing_line(server._agent_end_report_standing_line())

    assert "40 good" in line and "60 bad" in line
    assert "n=100" in line and "40.0%" in line


def test_a_sample_too_thin_to_measure_is_not_rendered_as_zero_percent(monkeypatch):
    """0 judged outcomes is ignorance, and ignorance still demands a check."""
    _record(monkeypatch, UNMEASURED_RECORD)

    line = _standing_line(server._agent_end_report_standing_line())

    assert "verify before claiming done: yes" in line
    assert "too few to measure" in line
    assert str(calibration.MIN_SAMPLE) in line
    assert "%" not in line


def test_an_unreadable_record_demands_verification_rather_than_passing(monkeypatch):
    """Ignorance fails closed in both directions."""
    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server, "_open_db_readonly", boom)

    line = _standing_line(server._agent_end_report_standing_line())

    assert "verify before claiming done: yes" in line
    assert "could not be read" in line


# --- it must not double-print what finish_final already says --------------


def test_the_standing_line_never_repeats_finish_finals_reason_sentence(monkeypatch):
    """One fact, one place. The prefix is about the run; this is the record."""
    for counts in (UNMEASURED_RECORD, POOR_RECORD, GOOD_RECORD):
        _record(monkeypatch, counts)
        _verify, reason = calibration.should_verify(_FakeConn(), "caller")

        line = _standing_line(server._agent_end_report_standing_line())

        assert reason not in line, "the reason prose belongs to finish_final"
        # And no fragment of it either -- the prefix owns that phrasing.
        assert "judged outcomes on record" not in line
        assert "so verify rather than assume" not in line


def test_the_standing_never_blends_the_two_populations(monkeypatch):
    """The execution population has no business on a line about caller work."""
    _record(monkeypatch, {"tests_passed": 8883, "failed": 182,
                          "accepted": 40, "rejected": 60})
    caller = calibration.measure(_FakeConn(), "caller")
    execution = calibration.measure(_FakeConn(), "execution")

    line = _standing_line(server._agent_end_report_standing_line())

    assert str(caller.total) in line
    assert str(execution.total) not in line
    assert "%.1f%%" % (execution.rate * 100) not in line
    # No arithmetic across the two, however it is spelled.
    assert str(caller.total + execution.total) not in line


# --- and it reaches the caller -------------------------------------------


def test_the_agent_end_report_shows_the_standing(monkeypatch):
    """The composed line is what a caller reading an agent report sees."""
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    _record(monkeypatch, POOR_RECORD)
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda prompt, history=None: '{"final":"done"}'),
    )

    out = server.agent("do the work", max_steps=1)

    assert "=== END REPORT ===" in out
    assert _standing_line(out).startswith("standing: verify before claiming done: yes")


# --- reading a standing must not write to the store -----------------------
#
# ``_open_db`` is the WRITE path. ``memory_store.connect`` runs
# ``PRAGMA journal_mode=WAL`` (a brief exclusive lock) and then ``init_db``
# under ``BEGIN IMMEDIATE``, with ``busy_timeout=30000`` set first. For a
# caller about to write that is exactly right. For one that wants a count it
# is not: it can create a ~200KB database from nothing, run schema migration,
# and wait up to thirty seconds behind another Sonder process's write lock --
# and a *wait* is not an exception, so no ``try`` around it can shorten it.
#
# ``/report`` is the surface where this bites. It did no I/O at all before the
# standing line was added, so the standing must reach it through a genuinely
# read-only open or it has made a status command capable of stalling.


def test_reading_a_standing_never_creates_the_store(monkeypatch, tmp_path):
    """A question about the record must not bring the record into existence."""
    missing = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_DB_PATH", str(missing))

    line = _standing_line(server._agent_end_report_standing_line())

    assert not missing.exists(), "reading a standing must not create a store"
    assert not list(tmp_path.iterdir()), "nor any sidecar: %r" % (
        [p.name for p in tmp_path.iterdir()],
    )
    # And it still fails closed: an absent record is not a passing record.
    assert "verify before claiming done: yes" in line
    assert "could not be read" in line


def test_the_standing_open_is_read_only_and_waits_seconds_not_half_a_minute(
    monkeypatch, tmp_path
):
    """Bounded, and unable to write even if something later tries."""
    import sqlite3

    import sonder_runtime.adapters.memory_store as memory_store

    path = tmp_path / "memory.db"
    writer = memory_store.connect(str(path))
    memory_store.record_outcome_row(writer, "i1", "accepted", 0.8, source="caller")
    writer.close()
    monkeypatch.setattr(server, "_DB_PATH", str(path))

    conn = server._open_db_readonly()
    try:
        assert conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1
        # A stall is not an exception. Thirty seconds is the write path's
        # budget; a status command does not get to borrow it.
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == server._READ_ONLY_BUSY_TIMEOUT_MS
        assert 0 < timeout <= 5000
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE scribble (x)")
    finally:
        conn.close()


# --- /report is a wired site, so it is a tested site ----------------------


def test_the_report_command_shows_the_standing(monkeypatch):
    """The other end-report surface. Untested is how a wired site regresses."""
    _record(monkeypatch, POOR_RECORD)
    with activity_tracker.response_span("test", "prompt", surface="agent"):
        activity_tracker.set_result_summary("did the thing")

    out = server.control_command("/report")

    assert "=== END REPORT ===" in out
    assert _standing_line(out).startswith(
        "standing: verify before claiming done: yes"
    )
    assert "40 good / 60 bad" in out


# --- both standings must read the SAME store ------------------------------
#
# ``_agent_verification_standing`` (via ``_open_db``) and this line (via
# ``_open_db_readonly``) render on the same page. sqlite3 resolves a relative
# path against the process cwd, but ``Path(p).as_uri()`` REFUSES a relative
# path outright -- so a relative ``SONDER_DB`` made one of them read 192 judged
# outcomes while the other said the record could not be read.
#
# Failing closed is a property of the verdict, not of the report. Two verdicts
# on one page that contradict each other is worse than an admission of
# ignorance, because a reader has to guess which half to believe.


def test_both_standings_read_the_same_store_under_a_relative_db_path(
    monkeypatch, tmp_path
):
    """One page, one answer -- whatever SONDER_DB happens to look like."""
    import sonder_runtime.adapters.memory_store as memory_store

    monkeypatch.chdir(tmp_path)
    writer = memory_store.connect("memory.db")
    for n in range(40):
        memory_store.record_outcome_row(writer, "ok%d" % n, "accepted", 0.8, source="caller")
    for n in range(60):
        memory_store.record_outcome_row(writer, "no%d" % n, "rejected", -0.5, source="caller")
    writer.close()
    # Relative, exactly as SONDER_DB=memory.db would leave it.
    monkeypatch.setattr(server, "_DB_PATH", "memory.db")

    line = _standing_line(server._agent_end_report_standing_line())
    demanded, reason = server._agent_verification_standing()

    assert "40 good / 60 bad" in line and "n=100" in line
    assert "could not be read" not in line
    # The other half of the page, from the other opener, on the same store.
    assert demanded is True
    assert "100 judged outcomes" in reason
