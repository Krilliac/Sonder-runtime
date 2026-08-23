"""Contracts for the /sessions listing renderer."""
import calendar
import time

from sonder_runtime.adapters.observability.session_list_formatting import (
    format_sessions,
    relative_age,
)


_NOW = calendar.timegm(time.strptime("2026-08-22 12:00:00", "%Y-%m-%d %H:%M:%S"))


def test_empty_listing_keeps_the_stable_empty_state_line():
    assert format_sessions([]) == "(no past sessions)"
    assert format_sessions(None) == "(no past sessions)"


def test_rows_lead_with_the_id_that_resume_and_replay_accept():
    out = format_sessions([{
        "session_id": "abc123", "turn_count": 4,
        "title": "fix the parser", "updated_ts": "2026-08-22 11:00:00",
        "project": "sonder",
    }], now=_NOW)
    line = out.splitlines()[0]
    assert line.split()[0] == "abc123"
    assert "[4 turns]" in line
    assert "1h ago" in line
    assert "fix the parser" in line
    assert "· sonder" in line


def test_untitled_and_unprojected_rows_stay_readable():
    out = format_sessions([{
        "session_id": "abc", "turn_count": 0, "title": None,
        "updated_ts": "", "project": "",
    }])
    assert "(untitled)" in out
    assert "·" not in out


def test_relative_age_covers_each_magnitude():
    assert relative_age("2026-08-22 11:59:30", now=_NOW) == "30s ago"
    assert relative_age("2026-08-22 11:30:00", now=_NOW) == "30m ago"
    assert relative_age("2026-08-22 02:00:00", now=_NOW) == "10h ago"
    assert relative_age("2026-08-12 12:00:00", now=_NOW) == "10d ago"


def test_unparseable_timestamps_are_shown_rather_than_guessed():
    assert relative_age("last tuesday", now=_NOW) == "last tuesday"
    assert relative_age("", now=_NOW) == ""


def test_clock_skew_never_yields_a_negative_age():
    assert relative_age("2026-08-22 12:05:00", now=_NOW) == "now"


def test_output_carries_no_ansi_escapes():
    out = format_sessions([{
        "session_id": "abc", "turn_count": 1, "title": "t",
        "updated_ts": "2026-08-22 11:00:00", "project": "p",
    }], now=_NOW)
    assert "\x1b" not in out
