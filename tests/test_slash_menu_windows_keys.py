"""Windows composer key map: Shift+Tab mode cycle, bracketed paste, enable_vt."""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.optional_slash_menu_impl as slash_menu


class _FakeMsvcrt:
    """Stands in for msvcrt: scripted getwch(); kbhit() true while keys remain
    that were marked as arriving in the same burst."""

    def __init__(self, keys, *, burst=False):
        self._keys = list(keys)
        self._burst = burst

    def getwch(self):
        if not self._keys:
            raise AssertionError("reader asked for more keys than were scripted")
        return self._keys.pop(0)

    def kbhit(self):
        return self._burst and bool(self._keys)


class _Out:
    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    @property
    def text(self):
        return "".join(self.chunks)


def _complete(prefix, limit=8):
    return []


def _drive(monkeypatch, keys, *, burst=False, vt=True, **kwargs):
    console = _FakeMsvcrt(keys, burst=burst)
    out = _Out()
    monkeypatch.setattr(slash_menu, "_msvcrt", lambda: console)
    monkeypatch.setattr(slash_menu.sys, "stdout", out)
    monkeypatch.setattr(slash_menu, "enable_vt", lambda: vt)
    line = slash_menu._read_line_raw("> ", completer=_complete, **kwargs)
    return line, out


# --- read_key: the pure key map -------------------------------------------


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
def test_shift_tab_scan_code_maps_to_mode_cycle(prefix):
    keys = [prefix, "\x0f"]
    fake = _FakeMsvcrt(keys)
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (
        slash_menu.KIND_KEY, slash_menu.KEY_MODE_CYCLE)


def test_vt_backtab_maps_to_mode_cycle():
    fake = _FakeMsvcrt(["\x1b", "[", "Z"], burst=True)
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (
        slash_menu.KIND_KEY, slash_menu.KEY_MODE_CYCLE)


@pytest.mark.parametrize("scan,token", [
    ("H", slash_menu.KEY_UP), ("P", slash_menu.KEY_DOWN),
    ("K", slash_menu.KEY_LEFT), ("M", slash_menu.KEY_RIGHT),
    ("G", slash_menu.KEY_HOME), ("O", slash_menu.KEY_END),
    ("S", slash_menu.KEY_DELETE),
])
def test_existing_scan_codes_still_map(scan, token):
    fake = _FakeMsvcrt(["\xe0", scan])
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (slash_menu.KIND_KEY, token)


def test_unknown_scan_code_is_ignored():
    fake = _FakeMsvcrt(["\x00", ";"])  # F1
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (slash_menu.KIND_IGNORE, "")


def test_lone_escape_is_escape():
    fake = _FakeMsvcrt(["\x1b"])
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (slash_menu.KIND_KEY, "\x1b")


def test_bracketed_paste_is_one_text_chunk():
    keys = list("\x1b[200~") + list("line one\r\nline two") + list("\x1b[201~")
    fake = _FakeMsvcrt(keys, burst=True)
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (
        slash_menu.KIND_PASTE, "line one\r\nline two")


def test_unknown_csi_is_swallowed_not_typed():
    fake = _FakeMsvcrt(list("\x1b[15~"), burst=True)
    assert slash_menu.read_key(fake.getwch, fake.kbhit) == (slash_menu.KIND_IGNORE, "")


# --- the raw reader ---------------------------------------------------------


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
def test_shift_tab_cycles_the_mode_and_keeps_the_line(monkeypatch, prefix):
    calls = []
    frames = iter(["code | auto", "code | plan"])
    line, out = _drive(
        monkeypatch, list("hi") + [prefix, "\x0f"] + list("!") + ["\r"],
        mode_cycle=lambda: calls.append("cycle") or "auto",
        refresh_frame=lambda: next(frames),
        frame="code | manual",
    )
    assert calls == ["cycle"]
    assert line == "hi!"
    assert "code | auto" in out.text


def test_default_mode_cycle_uses_the_mode_service_attended(monkeypatch):
    import permission_modes

    seen = []

    def fake_cycle(step=1):
        seen.append(permission_modes.mode_change_attended())
        return "acceptEdits"

    monkeypatch.setattr(permission_modes, "cycle_mode", fake_cycle)
    assert slash_menu.cycle_permission_mode() == "acceptEdits"
    assert seen == [True]


def test_shift_tab_uses_default_cycler_when_none_given(monkeypatch):
    calls = []
    monkeypatch.setattr(slash_menu, "cycle_permission_mode",
                        lambda: calls.append(1) or "auto")
    line, _ = _drive(monkeypatch, ["\x00", "\x0f", "x", "\r"])
    assert calls == [1] and line == "x"


def test_mode_service_failure_does_not_cost_the_prompt(monkeypatch):
    def boom():
        raise RuntimeError("store locked")

    line, _ = _drive(monkeypatch, ["\xe0", "\x0f", "o", "k", "\r"], mode_cycle=boom)
    assert line == "ok"


def test_bracketed_paste_newlines_do_not_submit(monkeypatch):
    keys = (list("\x1b[200~") + list("a\r\nb\x07c") + list("\x1b[201~")
            + ["\r"])
    # kbhit() reports the rest of the paste as queued, as a console does;
    # the final Enter is typed after it with nothing behind it.
    line, out = _drive(monkeypatch, keys, burst=True)
    # One message; the BEL is dropped, not typed.
    assert line == "a\nbc"
    assert out.text.startswith(slash_menu.BRACKETED_PASTE_ON)
    assert slash_menu.BRACKETED_PASTE_OFF in out.text
    assert out.text.endswith("\n")
    assert not out.text.endswith(slash_menu.BRACKETED_PASTE_OFF)


def test_paste_without_markers_keeps_burst_newlines(monkeypatch):
    # A console with no bracketed paste delivers "a<Enter>b" as keys in one
    # burst: the first Enter has input queued behind it, so it is a line
    # break; the last Enter arrives with nothing queued, so it submits.
    line, out = _drive(monkeypatch, list("a\rb\r"), burst=True, vt=False)
    assert line == "a\nb"
    assert slash_menu.BRACKETED_PASTE_ON not in out.text


def test_enter_on_an_empty_line_is_never_a_paste(monkeypatch):
    line, _ = _drive(monkeypatch, ["\r", "x"], burst=True)
    assert line == ""


def test_no_paste_mode_when_vt_is_unavailable(monkeypatch):
    line, out = _drive(monkeypatch, list("x") + ["\r"], vt=False)
    assert line == "x"
    assert "\x1b[?2004" not in out.text


def test_insert_text_sanitizes_controls():
    state = slash_menu.MenuState(completer=_complete)
    state.feed("ab")
    state.cursor = 1
    state.insert_text("X\x1b[31mY\tZ\r")
    assert state.buffer == "aX[31mY Z\nb"
    assert state.cursor == len("aX[31mY Z\n")


def test_multiline_buffer_renders_one_row_per_line():
    assert slash_menu._input_lines("> ", "a\nb", 80) == ["> a", "b"]
    assert slash_menu._wrap_cells("a\nb", 10) == ["a", "b"]
    row, col = slash_menu._cursor_cell("> ", "a\nbc", 4, 80, 2)
    assert (row, col) == (1, 2)


# --- enable_vt / supports_mode_cycle ---------------------------------------


def test_enable_vt_is_true_on_posix(monkeypatch):
    monkeypatch.setattr(slash_menu.os, "name", "posix")
    assert slash_menu.enable_vt() is True


def test_enable_vt_failure_path_returns_false(monkeypatch):
    monkeypatch.setattr(slash_menu.os, "name", "nt")
    monkeypatch.setattr(slash_menu, "_enable_windows_vt_output", lambda: False)
    assert slash_menu.enable_vt() is False


def test_enable_vt_never_raises(monkeypatch):
    monkeypatch.setattr(slash_menu.os, "name", "nt")

    def boom():
        raise OSError("no console")

    monkeypatch.setattr(slash_menu, "_enable_windows_vt_output", boom)
    assert slash_menu.enable_vt() is False


def test_enable_vt_success_path(monkeypatch):
    monkeypatch.setattr(slash_menu.os, "name", "nt")
    monkeypatch.setattr(slash_menu, "_enable_windows_vt_output", lambda: True)
    assert slash_menu.enable_vt() is True


def test_windows_vt_output_without_ctypes_windll_is_false(monkeypatch):
    # On a non-Windows interpreter ctypes has no windll: the real probe must
    # degrade to False rather than raise.
    monkeypatch.setattr(slash_menu.os, "name", "nt")
    assert slash_menu._enable_windows_vt_output() in (True, False)


def test_supports_mode_cycle_tracks_msvcrt(monkeypatch):
    try:
        import msvcrt  # noqa: F401
        expected = True
    except ImportError:
        expected = False
    assert slash_menu.supports_mode_cycle is expected
    assert isinstance(slash_menu.supports_mode_cycle, bool)
