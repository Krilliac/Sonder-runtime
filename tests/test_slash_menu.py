"""slash_menu must be testable, and safe, without a terminal.

Every test here runs headless: the key handling lives in a pure state machine
so the menu's behaviour can be driven with plain strings, and the terminal
paths are only checked for the one property that matters in CI -- that they
refuse to engage and fall through to builtin input().
"""
from __future__ import annotations

import builtins

import pytest

import slash_menu


class _Entry:
    """Stand-in for command_catalog.Command (only .name/.summary are read)."""

    def __init__(self, name, summary=""):
        self.name = name
        self.summary = summary


_COMMANDS = [
    _Entry("/help", "show help"),
    _Entry("/read", "read a file"),
    _Entry("/run", "run a command"),
    _Entry("/report", "write a report"),
    _Entry("/stats", "show statistics"),
]


def _completer(prefix, limit=8):
    """Name-prefix filter standing in for command_catalog.complete."""
    text = str(prefix or "").lstrip("/").lower()
    if not text:
        return _COMMANDS[:limit]
    return [c for c in _COMMANDS if c.name.lstrip("/").startswith(text)][:limit]


def _state(buffer=""):
    state = slash_menu.MenuState(completer=_completer, limit=8)
    state.feed(buffer)
    return state


# --- degradation ----------------------------------------------------------


def test_available_is_false_without_a_tty():
    # pytest replaces stdin with a non-tty capture object, which is exactly
    # the situation sonder_client and the suite put the REPL in.
    assert slash_menu.available() is False


def test_no_color_preserves_raw_keyboard_composer(monkeypatch):
    """NO_COLOR disables decoration, not completion/history/editing support."""
    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setattr(slash_menu.sys, "stdin", _Tty())
    monkeypatch.setattr(slash_menu.sys, "stdout", _Tty())
    monkeypatch.setattr(slash_menu, "_msvcrt", lambda: object())
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert slash_menu.available() is True


def test_available_is_false_on_a_dumb_terminal(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert slash_menu.available() is False


def test_available_survives_a_broken_stdin(monkeypatch):
    class _Exploding:
        def isatty(self):
            raise RuntimeError("no console")

    monkeypatch.setattr(slash_menu.sys, "stdin", _Exploding())
    assert slash_menu.available() is False


def test_read_line_falls_back_to_input_without_a_tty(monkeypatch):
    seen = []

    def _fake_input(prompt=""):
        seen.append(prompt)
        return "/help me"

    monkeypatch.setattr(builtins, "input", _fake_input)
    assert slash_menu.read_line("sonder> ") == "/help me"
    assert seen == ["sonder> "]


def test_read_line_falls_back_when_disabled(monkeypatch):
    monkeypatch.setattr(slash_menu, "available", lambda: True)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "plain")
    called = []
    monkeypatch.setattr(
        slash_menu, "_read_line_raw",
        lambda *a, **k: called.append(1) or "raw",
    )
    assert slash_menu.read_line("> ", enabled=False) == "plain"
    assert called == []


def test_framed_style_is_terminal_only_and_does_not_change_frame_geometry():
    state = slash_menu.MenuState(frame="Sonder", frame_style="\x1b[48;5;24m")
    styled = slash_menu._styled_frame("| input |", state.frame_style)

    assert styled == "\x1b[48;5;24m| input |\x1b[0m"
    assert slash_menu._ANSI_ESCAPE_RE.sub("", styled) == "| input |"


def test_terminal_size_prefers_the_visible_windows_console(monkeypatch):
    monkeypatch.setattr(slash_menu, "_windows_console_size", lambda: (144, 42))
    assert slash_menu._terminal_size() == (144, 42)


def test_raw_read_failure_falls_back_to_input(monkeypatch):
    """The whole point: a broken menu must not break the REPL."""
    monkeypatch.setattr(slash_menu, "available", lambda: True)

    def _explode(*args, **kwargs):
        raise RuntimeError("terminal went away")

    monkeypatch.setattr(slash_menu, "_read_line_raw", _explode)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "typed anyway")
    assert slash_menu.read_line("> ") == "typed anyway"


def test_keyboard_interrupt_from_raw_read_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(slash_menu, "available", lambda: True)

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(slash_menu, "_read_line_raw", _interrupt)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "should not run")
    with pytest.raises(KeyboardInterrupt):
        slash_menu.read_line("> ")


def test_default_completer_returns_a_list_when_the_catalog_is_unavailable(
    monkeypatch,
):
    """The catalog imports server; if that fails the menu is empty, not fatal."""
    import builtins as _builtins

    real_import = _builtins.__import__

    def _no_catalog(name, *args, **kwargs):
        if name == "command_catalog":
            raise ImportError("no catalog here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(_builtins, "__import__", _no_catalog)
    assert slash_menu._default_completer("/re") == []


# --- the menu only engages for slash lines --------------------------------


def test_plain_prose_never_opens_the_menu():
    state = _state("hello world")
    assert state.menu_active is False
    assert state.render_rows() == []
    assert state.matches() == []


def test_backspace_on_prose_behaves_like_normal_input():
    state = _state("hey")
    state.handle_key("\x08")
    assert state.buffer == "he"
    assert state.render_rows() == []


def test_left_right_home_end_and_delete_edit_in_the_middle_of_input():
    state = _state("abcd")
    state.handle_key(slash_menu.KEY_LEFT)
    state.handle_key(slash_menu.KEY_LEFT)
    state.handle_key("X")
    assert (state.buffer, state.cursor) == ("abXcd", 3)

    state.handle_key("\x08")
    assert (state.buffer, state.cursor) == ("abcd", 2)
    state.handle_key(slash_menu.KEY_DELETE)
    assert (state.buffer, state.cursor) == ("abd", 2)
    state.handle_key(slash_menu.KEY_HOME)
    assert state.cursor == 0
    state.handle_key(slash_menu.KEY_END)
    assert state.cursor == len(state.buffer)


def test_word_and_suffix_delete_preserve_other_mid_line_text():
    state = _state("keep this clause and this suffix")
    state.cursor = len("keep this clause and this")
    state.handle_key("\x17")  # Ctrl+W
    assert (state.buffer, state.cursor) == ("keep this clause and  suffix", 21)

    state.handle_key("\x0b")  # Ctrl+K
    assert (state.buffer, state.cursor) == ("keep this clause and ", 21)


def test_slash_opens_the_menu_on_the_popular_set():
    state = _state("/")
    assert state.menu_active is True
    rows = state.render_rows()
    assert len(rows) == len(_COMMANDS)
    assert rows[0].startswith(">")


# --- typing narrows -------------------------------------------------------


def test_typing_narrows_the_rows_in_place():
    state = _state("/")
    wide = state.render_rows()
    state.feed("re")
    narrow = state.render_rows()
    assert len(narrow) < len(wide)
    assert [c.name for c in state.matches()] == ["/read", "/report"]
    state.feed("a")
    assert [c.name for c in state.matches()] == ["/read"]


def test_backspace_widens_the_rows_again():
    state = _state("/read")
    assert len(state.matches()) == 1
    state.handle_key("\x08")
    assert state.buffer == "/rea"
    assert len(state.matches()) == 1
    state.handle_key("\x08")
    assert [c.name for c in state.matches()] == ["/read", "/report"]


def test_no_match_leaves_an_empty_menu_not_a_crash():
    state = _state("/zzzz")
    assert state.matches() == []
    assert state.render_rows() == []


# --- selection ------------------------------------------------------------


def test_down_moves_the_highlight():
    state = _state("/")
    state.handle_key(slash_menu.KEY_DOWN)
    assert state.selected == 1
    assert state.selection().name == "/read"
    assert state.render_rows()[1].startswith(">")


def test_up_and_down_clamp_at_the_ends():
    state = _state("/")
    state.handle_key(slash_menu.KEY_UP)
    assert state.selected == 0  # already at the top
    for _ in range(len(_COMMANDS) + 5):
        state.handle_key(slash_menu.KEY_DOWN)
    assert state.selected == len(_COMMANDS) - 1
    state.handle_key(slash_menu.KEY_UP)
    assert state.selected == len(_COMMANDS) - 2


def test_typing_resets_the_highlight_to_the_best_match():
    state = _state("/")
    state.handle_key(slash_menu.KEY_DOWN)
    state.handle_key(slash_menu.KEY_DOWN)
    assert state.selected == 2
    state.feed("r")
    assert state.selected == 0
    assert state.selection().name == "/read"


def test_arrows_are_inert_without_a_menu():
    state = _state("plain text")
    state.handle_key(slash_menu.KEY_DOWN)
    assert state.selected == 0
    assert state.buffer == "plain text"


# --- accepting ------------------------------------------------------------


def test_tab_replaces_the_buffer_with_the_selection():
    state = _state("/re")
    state.handle_key(slash_menu.KEY_DOWN)
    assert state.selection().name == "/report"
    assert state.handle_key("\t") == slash_menu.CONTINUE
    assert state.buffer == "/report"
    assert state.selected == 0


def test_tab_with_no_match_leaves_the_buffer_alone():
    state = _state("/zzz")
    state.handle_key("\t")
    assert state.buffer == "/zzz"


def test_argument_context_renders_usage_instead_of_selectable_completion():
    state = slash_menu.MenuState(
        completer=_completer,
        hint_provider=lambda buffer: "usage for %s" % buffer,
    )
    state.feed("/read notes.txt")

    assert state.argument_context is True
    assert state.has_palette_matches() is False
    assert state.selection() is None
    assert state.render_rows(width=200) == ["  usage for /read notes.txt"]


def test_argument_context_does_not_replace_input_on_tab():
    state = slash_menu.MenuState(
        completer=_completer, hint_provider=lambda _buffer: "usage",
    )
    state.feed("/read notes.txt")
    state.handle_key("\t")
    assert state.buffer == "/read notes.txt"


def test_bounded_argument_completer_renders_and_completes_model_choices():
    calls = []

    def choices(command, prefix, *, limit):
        calls.append((command, prefix, limit))
        return ["gemma3:12b", "gemma3:4b"]

    state = slash_menu.MenuState(completer=_completer, argument_completer=choices)
    state.feed("/model gem")

    assert state.has_palette_matches() is True
    assert state.render_rows(width=200) == [
        "> gemma3:12b  ", "  gemma3:4b   ",
    ]
    state.handle_key(slash_menu.KEY_DOWN)
    state.handle_key("\t")
    assert state.buffer == "/model gemma3:4b"
    assert calls == [("/model", "gem", 8)]


def test_bounded_argument_completer_never_claims_free_form_second_arguments():
    calls = []
    state = slash_menu.MenuState(
        completer=_completer,
        argument_completer=lambda *args, **kwargs: calls.append((args, kwargs)) or ["x"],
        hint_provider=lambda _buffer: "usage",
    )
    state.feed("/model gemma extra")

    assert state.argument_matches() == []
    assert state.render_rows(width=200) == ["  usage"]
    assert calls == []


def test_enter_accepts_the_line_as_typed():
    state = _state("/read notes.txt")
    assert state.handle_key("\r") == slash_menu.ACCEPT
    assert state.buffer == "/read notes.txt"


def test_ctrl_c_reports_interrupt():
    state = _state("/re")
    assert state.handle_key("\x03") == slash_menu.INTERRUPT


def test_ctrl_u_clears_the_line():
    state = _state("/read something")
    assert state.handle_key("\x15") == slash_menu.CONTINUE
    assert state.buffer == ""
    assert state.menu_active is False


# --- escape ---------------------------------------------------------------


def test_esc_dismisses_the_menu_but_keeps_the_line():
    state = _state("/re")
    state.handle_key("\x1b")
    assert state.buffer == "/re"
    assert state.menu_active is False
    assert state.render_rows() == []
    state.feed("ad")
    assert state.buffer == "/read"
    assert state.render_rows() == []  # stays dismissed while typing


def test_clearing_the_line_re_arms_the_menu():
    state = _state("/re")
    state.handle_key("\x1b")
    for _ in range(3):
        state.handle_key("\x08")
    assert state.buffer == ""
    state.feed("/r")
    assert state.menu_active is True
    assert state.render_rows()


# --- rendering ------------------------------------------------------------


def test_rows_are_truncated_to_the_terminal_width():
    state = _state("/")
    rows = state.render_rows(width=20)
    assert rows, "expected a menu"
    assert all(len(row) <= 19 for row in rows), rows
    assert any(row.endswith("…") for row in rows)


def test_long_input_is_wrapped_without_a_display_cap():
    message = "x" * 200

    lines = slash_menu._input_lines("sonder > ", message, width=20)

    assert "".join(lines) == "sonder > " + message
    assert all(len(line) <= 19 for line in lines)
    assert "..." not in "".join(lines)


def test_cursor_cell_tracks_wrapped_input_without_terminal_autowrap():
    # 19 visible cells at width 20; the prompt consumes two of them.
    assert slash_menu._cursor_cell(
        "> ", "x" * 20, 17, 20, 2,
    ) == (1, 0)


def test_unframed_input_wraps_cjk_emoji_and_combining_marks_by_display_cells():
    # Width 7 reserves its last cell, leaving six.  The prompt consumes two;
    # each of the CJK glyph and emoji consumes two, while the combining acute
    # stays with ``e`` without claiming another terminal cell.
    lines = slash_menu._input_lines("> ", "\u754c\U0001f642e\u0301x", width=7)

    assert lines == ["> \u754c\U0001f642", "e\u0301x"]


def test_unframed_cursor_uses_display_cells_not_codepoint_count():
    # CJK and emoji each use two cells; the combining acute uses zero.  This
    # verifies cursor placement independently of the wrapped text renderer.
    assert slash_menu._cursor_cell(
        "> ", "\u754c\U0001f642e\u0301x", 4, 20, 1,
    ) == (0, 7)


def test_live_redraw_keeps_only_a_viewport_sized_tail_of_wrapped_input():
    lines = [str(index) for index in range(10)]

    visible, start = slash_menu._visible_input_lines(
        lines, cursor_row=9, height=5, menu_rows=2,
    )

    assert visible == ["8", "9"]
    assert len(visible) == 2
    assert start == 8


def test_raw_cleanup_returns_to_first_wrapped_row_before_erasing():
    state = slash_menu.MenuState(buffer="/" + "x" * 80)
    state._drawn_input_rows = 4
    state._drawn_cursor_row = 3
    out = _FakeStdout()

    slash_menu._clear_raw_input(state, out)

    assert out.text == "\r" + slash_menu.CSI + "3A" + slash_menu.CSI + "0J"


def test_rows_never_exceed_the_terminal_height():
    entries = [_Entry("/c%d" % i, "summary %d" % i) for i in range(40)]
    state = slash_menu.MenuState(completer=lambda p, limit=8: entries[:limit])
    state.feed("/c")
    assert len(state.render_rows(height=100)) <= slash_menu.MAX_ROWS
    assert len(state.render_rows(height=5)) == 3   # height - 2
    assert state.render_rows(height=2) == []


def test_navigation_and_tab_never_select_a_height_clipped_row():
    entries = [_Entry("/c%d" % i, "summary %d" % i) for i in range(8)]
    state = slash_menu.MenuState(completer=lambda _p, limit=8: entries[:limit])
    state.feed("/c")
    assert len(state.render_rows(width=200, height=5)) == 3

    for _ in range(8):
        state.handle_key(slash_menu.KEY_DOWN)

    assert state.selected == 2
    assert state.selection() is entries[2]
    state.handle_key("\t")
    assert state.buffer == "/c2"


def test_render_rows_accepts_an_explicit_prefix():
    state = _state("")
    rows = state.render_rows("/rea", width=200)
    assert len(rows) == 1
    assert "/read" in rows[0]
    assert state.buffer == "", "render_rows must not mutate the state"


def test_render_rows_marks_exactly_one_selection():
    state = _state("/")
    state.handle_key(slash_menu.KEY_DOWN)
    rows = state.render_rows(width=200)
    assert sum(1 for row in rows if row.startswith(">")) == 1


def test_a_broken_completer_yields_an_empty_menu():
    def _broken(prefix, limit=8):
        raise RuntimeError("catalog exploded")

    state = slash_menu.MenuState(completer=_broken)
    state.feed("/re")
    assert state.matches() == []
    assert state.render_rows() == []
    assert state.buffer == "/re"


def test_unknown_control_keys_are_ignored_except_the_standard_clear_shortcut():
    state = _state("/re")
    state.handle_key("\x0e")  # Ctrl+N
    assert state.buffer == "/re"


def test_ctrl_l_requests_a_clear_without_discarding_input():
    state = _state("/read notes.txt")
    assert state.handle_key("\x0c") == slash_menu.CLEAR
    assert state.buffer == "/read notes.txt"


# --- the raw reader, driven through a fake console ------------------------


class _FakeConsole:
    """Stands in for msvcrt: getwch() replays a scripted key sequence."""

    def __init__(self, keys):
        self._keys = list(keys)

    def getwch(self):
        if not self._keys:
            raise AssertionError("reader asked for more keys than were scripted")
        return self._keys.pop(0)


class _FakeStdout:
    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.chunks)


def _drive(monkeypatch, keys, prompt="> ", history=None, frame=""):
    console = _FakeConsole(keys)
    out = _FakeStdout()
    monkeypatch.setattr(slash_menu, "_msvcrt", lambda: console)
    monkeypatch.setattr(slash_menu.sys, "stdout", out)
    return slash_menu._read_line_raw(
        prompt, completer=_completer, history=history, frame=frame), out


def test_raw_reader_accepts_a_typed_line(monkeypatch):
    line, out = _drive(monkeypatch, list("/read x") + ["\r"])
    assert line == "/read x"
    assert out.text.endswith("> /read x\n")


def test_raw_reader_decodes_the_arrow_prefix_and_completes_with_tab(monkeypatch):
    # \xe0 + 'P' is Down on Windows; Tab then takes the second match.
    keys = list("/re") + ["\xe0", "P", "\t", "\r"]
    line, _ = _drive(monkeypatch, keys)
    assert line == "/report"


def test_raw_reader_handles_cursor_edit_scan_codes(monkeypatch):
    keys = list("ac") + ["\xe0", "K", "b", "\xe0", "G", "\xe0", "S", "\r"]
    line, _ = _drive(monkeypatch, keys)
    assert line == "bc"


def test_raw_reader_recalls_session_history_outside_the_slash_palette(monkeypatch):
    line, _ = _drive(
        monkeypatch, ["\xe0", "H", "\r"], history=["one", "two"])
    assert line == "two"


def test_raw_reader_down_restores_the_unfinished_draft(monkeypatch):
    line, _ = _drive(
        monkeypatch,
        list("draft") + ["\xe0", "H", "\xe0", "P", "\r"],
        history=["older"],
    )
    assert line == "draft"


def test_raw_reader_reverse_searches_session_history_without_persisting(monkeypatch):
    line, _ = _drive(
        monkeypatch, list("build") + ["\x12", "\x12", "\r"],
        history=["open readme", "build release", "build debug"],
    )

    # First Ctrl+R finds the newest match; repeated Ctrl+R walks older matches
    # using the original "build" term rather than the recalled full command.
    assert line == "build release"


def test_reverse_search_restarts_after_an_edit(monkeypatch):
    line, _ = _drive(
        monkeypatch, list("build") + ["\x12", "\x15"] + list("build!") + ["\x12", "\r"],
        history=["build! release", "build debug"],
    )

    assert line == "build! release"


def test_raw_reader_recall_works_for_slash_text_without_palette_matches(monkeypatch):
    line, _ = _drive(
        monkeypatch, list("/usr/local/bin") + ["\xe0", "H", "\r"],
        history=["ordinary history"],
    )
    assert line == "ordinary history"


def test_raw_reader_editing_recalled_input_keeps_the_saved_draft(monkeypatch):
    line, _ = _drive(
        monkeypatch,
        list("draft") + ["\xe0", "H", "!", "\xe0", "P", "\r"],
        history=["older"],
    )
    assert line == "draft"


def test_raw_reader_uses_history_after_a_command_enters_argument_context(monkeypatch):
    line, _ = _drive(
        monkeypatch, list("/read notes.txt") + ["\xe0", "H", "\r"],
        history=["previous request"],
    )
    assert line == "previous request"


def test_raw_reader_ignores_an_unmapped_extended_key(monkeypatch):
    # \x00 + 'R' is Insert: it must be consumed whole, not leak an 'R'.
    keys = ["/", "\x00", "R", "h", "\r"]
    line, _ = _drive(monkeypatch, keys)
    assert line == "/h"


def test_raw_reader_raises_keyboard_interrupt_on_ctrl_c(monkeypatch):
    with pytest.raises(KeyboardInterrupt):
        _drive(monkeypatch, list("/re") + ["\x03"])


def test_raw_reader_ctrl_l_clears_screen_and_keeps_typed_input(monkeypatch):
    line, out = _drive(monkeypatch, list("draft") + ["\x0c", "\r"])
    assert line == "draft"
    assert slash_menu.CSI + "2J" + slash_menu.CSI + "H" in out.text


def test_raw_reader_clears_the_menu_before_returning(monkeypatch):
    _, out = _drive(monkeypatch, list("/re") + ["\r"])
    tail = out.text.split("\r")[-1]
    # The final write is an erase-to-end-of-display plus the accepted line, so
    # nothing of the menu survives underneath it.
    assert tail.startswith(slash_menu.CSI + "0J")
    assert "/read" not in tail  # the menu rows are gone
    assert tail.endswith("> /re\n")


def test_raw_reader_moves_the_cursor_back_onto_the_input_line(monkeypatch):
    _, out = _drive(monkeypatch, list("/re") + ["\r"])
    text = out.text
    rows = _state("/re").render_rows()
    assert rows, "expected the fixture to produce a menu"
    assert slash_menu.CSI + "%dA" % len(rows) in text
    assert slash_menu.CSI + "%dC" % len("> /re") in text


def test_framed_raw_reader_draws_a_full_composer_and_keeps_the_line(monkeypatch):
    line, out = _drive(
        monkeypatch, list("hello") + ["\r"], prompt="", frame="sonder [lanes 2 | agents 1]")

    assert line == "hello"
    # Frame punctuation is deliberately presentation only: the accepted
    # buffer is exactly the text typed by the user.
    assert "sonder [lanes 2 | agents 1]" in out.text
    assert "Enter send" in out.text
    assert "hello" in out.text
    assert any(token in out.text for token in ("╭", "+"))


def test_framed_palette_sits_above_composer_with_its_own_controls(monkeypatch):
    _line, out = _drive(monkeypatch, list("/re") + ["\r"], prompt="", frame="sonder")

    text = out.text
    # The menu is presented before the composer top edge, not below its
    # footer, and its controls describe selection rather than generic send.
    assert text.rfind("> /read") < text.rfind("╭")
    assert "Tab complete" in text
    assert "Up/Down select" in text


def test_framed_composer_uses_a_stable_rectangle_without_content_truncation():
    out = _FakeStdout()
    top, rows, footer = slash_menu._framed_input_lines(
        "sonder [lanes 2 | agents 1]", "x" * 60, 20, out)

    assert len(top) == len(footer) == 19
    assert all(len(row) == 19 for row in rows)
    assert "".join(row[4:-1].rstrip() for row in rows) == "x" * 60


def test_framed_composer_keeps_unicode_text_inside_its_cell_rectangle():
    out = _FakeStdout()
    top, rows, footer = slash_menu._framed_input_lines(
        "Sonder 🤖 東京", "🚀東京́" * 4, 20, out)

    assert slash_menu._display_width(top) == 19
    assert slash_menu._display_width(footer) == 19
    assert all(slash_menu._display_width(row) == 19 for row in rows)
    assert "".join(row[4:-1].rstrip() for row in rows) == "🚀東京́" * 4


def test_emoji_graphemes_are_one_two_cell_glyph_for_layout_and_clipping():
    for glyph in ("👩\u200d💻", "👋🏽", "❤️", "🇺🇸"):
        assert slash_menu._display_width(glyph) == 2
        assert slash_menu._clip_cells(glyph + "x", 2) == glyph
        assert slash_menu._wrap_cells(glyph + "x", 2) == [glyph, "x"]


def test_framed_cursor_uses_cells_for_wide_unicode_at_a_wrap_boundary():
    # The 11-column frame has six editable cells.  Three CJK glyphs fill it
    # exactly, so the cursor remains at the first row's right edge.
    assert slash_menu._framed_cursor_cell("東京大", 3, 12, 1) == (0, 6)


def test_framed_composer_tracks_a_cursor_in_a_wrapped_buffer(monkeypatch):
    monkeypatch.setattr(slash_menu, "_terminal_size", lambda: (12, 12))
    # 6 editable cells per row in the 11-column visual frame. Move left into
    # the earlier row, insert there, and prove redraw keeps the entire value.
    keys = list("abcdefghijkl") + ["\xe0", "K", "\xe0", "K", "\xe0", "K", "\xe0", "K", "X", "\r"]
    line, out = _drive(monkeypatch, keys, prompt="", frame="sonder")

    assert line == "abcdefghXijkl"
    assert "abcdef" in out.text and "ghXijk" in out.text


def test_framed_cursor_stays_at_row_end_at_an_exact_wrap_boundary():
    # 12 terminal columns yield 6 editable cells after the chat prompt marker.
    assert slash_menu._framed_cursor_cell("x" * 6, 6, 12, 1) == (0, 6)
