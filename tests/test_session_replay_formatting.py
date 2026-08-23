"""Contracts for the /replay transcript renderer."""
from sonder_runtime.adapters.observability import response_formatting
from sonder_runtime.adapters.observability.session_replay_formatting import (
    REPLAY_DEFAULT_TURNS,
    REPLAY_MAX_TURNS,
    REPLAY_TEXT_LIMIT,
    clamp_turn_limit,
    clean_response,
    format_session_replay,
)


def _turn(task, response):
    return {"id": "i1", "task": task, "response": response}


def test_empty_thread_renders_a_stable_empty_state():
    out = format_session_replay([], session_id="abc123")
    assert out.splitlines()[0].startswith("replay abc123")
    assert "(no stored turns in this thread yet)" in out


def test_turns_render_oldest_first_with_aligned_speaker_prefixes():
    out = format_session_replay(
        [_turn("first question", "first answer"),
         _turn("second question", "second answer")],
        session_id="abc123",
    )
    lines = out.splitlines()
    assert lines[0] == "replay abc123  ·  2 turn(s)"
    assert "[1] you    | first question" in lines
    assert "    sonder | first answer" in lines
    assert "[2] you    | second question" in lines
    assert out.index("first question") < out.index("second question")


def test_multiline_text_hang_indents_under_its_speaker_prefix():
    out = format_session_replay(
        [_turn("line one\nline two", "answer")], session_id="s",
    )
    assert "[1] you    | line one" in out
    # Continuation aligns under the text, not under the prefix.
    assert "\n" + (" " * len("[1] you    | ")) + "line two" in out


def test_stored_footer_trace_and_activity_chrome_are_stripped():
    stored = (
        "the answer"
        "\n\n=== ACTIVITY (observable work) ===\nstuff\n=== END ACTIVITY ==="
        + response_formatting.FOOTER_PREFIX + "abcdef]"
    )
    cleaned = clean_response(stored)
    assert cleaned == "the answer"

    traced = (
        "answer text\n"
        "=== TRACE (how Sonder Runtime decided) ===\nprompt dump\n=== END TRACE ==="
    )
    assert clean_response(traced) == "answer text"


def test_replay_shows_only_the_last_limit_turns_and_says_so():
    turns = [_turn("q%d" % n, "a%d" % n) for n in range(1, 6)]
    out = format_session_replay(turns, session_id="s", limit=2)
    assert "5 turn(s)" in out
    assert "showing last 2" in out
    assert "q4" in out and "q5" in out
    assert "q1" not in out
    # Turn numbering keeps its absolute position in the thread.
    assert "[4] you    | q4" in out


def test_oversized_fields_are_bounded_with_an_omission_note():
    big = "x" * (REPLAY_TEXT_LIMIT + 250)
    out = format_session_replay([_turn(big, "ok")], session_id="s")
    assert "... (+250 more characters)" in out
    assert big not in out


def test_turn_limit_is_clamped_to_the_supported_window():
    assert clamp_turn_limit(None) == REPLAY_DEFAULT_TURNS
    assert clamp_turn_limit("nope") == REPLAY_DEFAULT_TURNS
    assert clamp_turn_limit(0) == 1
    assert clamp_turn_limit(10_000) == REPLAY_MAX_TURNS


def test_output_carries_no_ansi_escapes():
    out = format_session_replay(
        [_turn("q", "a")], session_id="s",
    )
    assert "\x1b" not in out
