"""slash_menu -- a live, filtering command palette for the REPL's input line.

Typing ``/`` opens a menu of the most-used commands underneath the cursor;
every further character narrows it in place, Up/Down move the highlight, and
Tab completes the line to the highlighted entry.  It is the ``/`` palette from
Claude Code, driven by :mod:`command_catalog` so the menu can never drift from
the commands that actually exist.

Two rules shaped the design more than any feature:

* **The REPL must survive this module being wrong.**  A command line is the
  only way into the process, so a menu that raises, hangs, or scribbles over
  the prompt is worse than no menu at all.  Every raw-terminal path is wrapped
  and falls back to builtin :func:`input`; :func:`available` refuses to engage
  at all when stdin is not a terminal, which is how the test suite and
  ``sonder_client`` drive the REPL.
* **The key handling is a pure state machine.**  :class:`MenuState` holds the
  buffer and the highlight and knows nothing about terminals, so the whole
  interaction is testable headlessly.  Only :func:`_read_line_raw` touches
  ``msvcrt`` and ANSI, and it is a thin shell over the state machine.

Stdlib only.  ``command_catalog`` is imported lazily: it imports ``server`` to
build the catalog, which is far too much to pay for a module that may never be
asked to draw anything.
"""
from __future__ import annotations

import os
import re
import shutil
import sys

# --- key tokens and actions ----------------------------------------------

# Arrow keys arrive from msvcrt.getwch() as a two-character sequence, so the
# reader translates them into these tokens before handing them to the state
# machine.  They are spelled with angle brackets so they can never collide with
# a character the user could actually type.
KEY_UP = "<up>"
KEY_DOWN = "<down>"
KEY_LEFT = "<left>"
KEY_RIGHT = "<right>"
KEY_HOME = "<home>"
KEY_END = "<end>"
KEY_DELETE = "<delete>"

# handle_key's return values.
CONTINUE = "continue"   # keep reading; the caller should repaint
ACCEPT = "accept"       # the line is finished
INTERRUPT = "interrupt"  # Ctrl+C: the caller should raise KeyboardInterrupt
CLEAR = "clear"          # Ctrl+L: clear screen but retain the input buffer

_ENTER = ("\r", "\n")
_BACKSPACE = ("\x08", "\x7f")
_TAB = "\t"
_ESC = "\x1b"
_CTRL_C = "\x03"
_CTRL_L = "\x0c"
_CTRL_U = "\x15"
_CTRL_W = "\x17"
_CTRL_K = "\x0b"

MAX_ROWS = 8
HISTORY_LIMIT = 200

CSI = "\x1b["
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# --- the pure state machine ----------------------------------------------


def _default_completer(prefix, limit=MAX_ROWS):
    """Ask the catalog what matches; never let its import cost the REPL."""
    try:
        import command_catalog
        return list(command_catalog.complete(prefix, limit=limit))
    except Exception:
        return []


def _name_of(entry) -> str:
    return str(getattr(entry, "name", entry) or "")


def _summary_of(entry) -> str:
    return str(getattr(entry, "summary", "") or "")


def _default_argument_hint(buffer: str) -> str:
    """Return a compact usage hint once a complete command has an argument.

    Kept lazy for the same reason as the completion catalog: a terminal
    convenience must never make importing the REPL expensive or fragile.
    """
    try:
        command_name = str(buffer or "").split(None, 1)[0]
        if not command_name or command_name == "/":
            return ""
        import command_catalog
        command = command_catalog.by_name(command_name)
        if command is None:
            return ""
        usage = command.usage()
        summary = command.summary or ""
        return "%s  %s" % (usage, summary)
    except Exception:
        return ""


class MenuState:
    """Buffer + highlight for one input line.  No terminal, no I/O.

    ``handle_key`` takes one key (a printable character, a control character,
    or one of the ``KEY_*`` tokens) and returns one of :data:`CONTINUE`,
    :data:`ACCEPT`, :data:`INTERRUPT`.  ``render_rows`` turns the current
    state into the exact lines to draw.  Everything the reader needs is in
    those two methods, which is what makes the interaction testable without a
    terminal.
    """

    def __init__(self, completer=None, limit: int = MAX_ROWS, buffer: str = "",
                 hint_provider=None):
        self.completer = completer or _default_completer
        self.hint_provider = hint_provider or _default_argument_hint
        self.limit = max(1, int(limit))
        self.buffer = str(buffer or "")
        self.cursor = len(self.buffer)
        self.selected = 0
        self.dismissed = False
        self._cache_key = None
        self._cache: list = []
        # Rendering state only.  The pure key/menu state above never relies on
        # it; retaining the number of drawn physical rows lets the raw reader
        # erase a wrapped message before redrawing it after the next key.
        self._drawn_input_rows = 1

    # -- queries ----------------------------------------------------------

    @property
    def menu_active(self) -> bool:
        """True when a menu should be on screen for the current buffer."""
        return self.buffer.startswith("/") and not self.dismissed

    def matches(self) -> list:
        """Catalog entries for the current buffer, memoised per buffer.

        A misbehaving completer yields an empty menu rather than killing the
        line the user is in the middle of typing.
        """
        if not self.buffer.startswith("/"):
            return []
        if self._cache_key != self.buffer:
            try:
                found = list(self.completer(self.buffer, limit=self.limit))
            except Exception:
                found = []
            self._cache_key = self.buffer
            self._cache = found[: self.limit]
        return self._cache

    def selection(self):
        """The highlighted entry, or None when nothing is highlighted."""
        if self.argument_context:
            return None
        rows = self.matches()
        if not rows:
            return None
        return rows[max(0, min(self.selected, len(rows) - 1))]

    @property
    def argument_context(self) -> bool:
        """Whether the buffer is past a known command's name boundary."""
        text = self.buffer
        if not text.startswith("/") or text == "/":
            return False
        return any(ch.isspace() for ch in text)

    def has_palette_matches(self) -> bool:
        """Whether arrows should select a completion instead of recall history."""
        return not self.argument_context and bool(self.matches())

    # -- transitions ------------------------------------------------------

    def _reset_selection(self) -> None:
        self.selected = 0

    def handle_key(self, ch: str) -> str:
        if ch == KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
            return CONTINUE
        if ch == KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
            return CONTINUE
        if ch == KEY_HOME:
            self.cursor = 0
            return CONTINUE
        if ch == KEY_END:
            self.cursor = len(self.buffer)
            return CONTINUE
        if ch == KEY_DELETE:
            self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]
            self._reset_selection()
            return CONTINUE
        if ch == KEY_UP:
            if self.menu_active and self.has_palette_matches():
                # Clamps rather than wrapping: wrapping from the first entry to
                # the last is a surprise when the list is being retyped under
                # you, and the top entry is the one you usually want.
                self.selected = max(0, self.selected - 1)
            return CONTINUE
        if ch == KEY_DOWN:
            rows = self.matches() if self.menu_active and not self.argument_context else []
            if rows:
                self.selected = min(len(rows) - 1, self.selected + 1)
            return CONTINUE
        if ch == _CTRL_C:
            return INTERRUPT
        if ch == _CTRL_L:
            return CLEAR
        if ch == _CTRL_U:
            self.buffer = ""
            self.cursor = 0
            self.dismissed = False
            self._reset_selection()
            return CONTINUE
        if ch == _CTRL_W:
            # Match common shell/readline behavior: erase whitespace first,
            # then the preceding word, while keeping everything after the
            # cursor untouched for mid-line editing.
            start = self.cursor
            while start and self.buffer[start - 1].isspace():
                start -= 1
            while start and not self.buffer[start - 1].isspace():
                start -= 1
            self.buffer = self.buffer[:start] + self.buffer[self.cursor:]
            self.cursor = start
            self._reset_selection()
            return CONTINUE
        if ch == _CTRL_K:
            # Delete only the editable suffix; this is especially useful for
            # a recalled long prompt where the cursor was moved to a clause.
            self.buffer = self.buffer[:self.cursor]
            self._reset_selection()
            return CONTINUE
        if ch in _ENTER:
            return ACCEPT
        if ch == _ESC:
            # Dismiss, but keep the line: Esc means "stop showing me that",
            # not "throw away what I typed".
            self.dismissed = True
            self._reset_selection()
            return CONTINUE
        if ch == _TAB:
            if self.menu_active:
                entry = self.selection()
                if entry is not None:
                    self.buffer = _name_of(entry)
                    self.cursor = len(self.buffer)
                    self._reset_selection()
            return CONTINUE
        if ch in _BACKSPACE:
            if self.cursor:
                self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
                self.cursor -= 1
            if not self.buffer:
                # An empty line is a fresh start, so a later "/" re-opens the
                # menu even after Esc.
                self.dismissed = False
            self._reset_selection()
            return CONTINUE
        if len(ch) == 1 and (ch >= " " and ch != "\x7f"):
            self.buffer = self.buffer[:self.cursor] + ch + self.buffer[self.cursor:]
            self.cursor += 1
            self._reset_selection()
            return CONTINUE
        # Unknown control key: ignore it rather than inserting a glyph.
        return CONTINUE

    def feed(self, text: str) -> str:
        """Apply a string of keys in order (test/helper convenience)."""
        action = CONTINUE
        for ch in text:
            action = self.handle_key(ch)
        return action

    # -- rendering --------------------------------------------------------

    def render_rows(self, prefix=None, width: int = 0, height: int = 0) -> list:
        """The lines to draw under the input, already truncated to width.

        Truncation is not cosmetic: a row wider than the terminal wraps, and a
        wrapped row makes the "move back up N lines" of the redraw land in the
        wrong place and eat the prompt.
        """
        if prefix is not None and prefix != self.buffer:
            probe = MenuState(
                self.completer, self.limit, buffer=str(prefix),
                hint_provider=self.hint_provider,
            )
            probe.selected = self.selected
            return probe.render_rows(width=width, height=height)
        if not self.menu_active:
            return []
        if self.argument_context:
            try:
                hint = str(self.hint_provider(self.buffer) or "")
            except Exception:
                hint = ""
            return [_truncate("  " + hint, width)] if hint else []
        entries = self.matches()
        if not entries:
            return []
        cols, lines = _terminal_size()
        width = int(width) if width else cols
        height = int(height) if height else lines
        # Never claim more rows than the screen has: the reader has to move the
        # cursor back up over exactly this many lines.
        budget = max(0, min(self.limit, MAX_ROWS, height - 2))
        entries = entries[:budget]
        if not entries:
            return []
        namewidth = max(len(_name_of(e)) for e in entries)
        picked = max(0, min(self.selected, len(entries) - 1))
        rows = []
        for index, entry in enumerate(entries):
            marker = ">" if index == picked else " "
            text = "%s %-*s  %s" % (
                marker, namewidth, _name_of(entry), _summary_of(entry),
            )
            rows.append(_truncate(text, width))
        return rows


class HistoryCursor:
    """Session-local command recall for the raw Windows reader.

    The menu owns arrows while a slash palette is visible. Everywhere else
    Up walks prior submitted lines and Down returns through them to the draft.
    History is supplied by the caller and is never written to disk.
    """

    def __init__(self, entries=None):
        self.entries = [str(entry) for entry in (entries or []) if str(entry)]
        self.index = len(self.entries)
        self.draft = ""

    def up(self, current: str) -> str:
        if not self.entries:
            return current
        if self.index == len(self.entries):
            self.draft = str(current)
        if self.index > 0:
            self.index -= 1
        return self.entries[self.index]

    def down(self, current: str) -> str:
        if not self.entries or self.index == len(self.entries):
            return current
        self.index += 1
        if self.index == len(self.entries):
            return self.draft
        return self.entries[self.index]

    def reset(self) -> None:
        self.index = len(self.entries)
        self.draft = ""


def _truncate(text: str, width: int) -> str:
    # width - 1: writing into the final column makes some terminals wrap
    # eagerly, which corrupts the redraw exactly like an over-long row does.
    limit = max(1, int(width) - 1)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _terminal_size():
    try:
        size = shutil.get_terminal_size((80, 24))
        return max(20, size.columns), max(4, size.lines)
    except Exception:
        return 80, 24


# --- availability ---------------------------------------------------------


def _msvcrt():
    import msvcrt  # noqa: F401  (import is the probe)
    return msvcrt


def available() -> bool:
    """True when a live menu can actually be drawn on this terminal.

    False for every reason a raw read would be wrong -- piped or redirected
    stdin (how the test suite and sonder_client drive the REPL), a non-Windows
    interpreter with no ``msvcrt``, a dumb terminal, or ``NO_COLOR`` -- so the
    caller silently gets plain :func:`input` instead of a broken prompt.
    """
    try:
        if os.environ.get("SONDER_NO_MENU"):
            return False
        if os.environ.get("NO_COLOR"):
            return False
        if (os.environ.get("TERM") or "").lower() in ("dumb", "unknown"):
            return False
        stdin = sys.stdin
        stdout = sys.stdout
        if stdin is None or stdout is None:
            return False
        if not (stdin.isatty() and stdout.isatty()):
            return False
        _msvcrt()
        return True
    except Exception:
        return False


# --- the raw reader -------------------------------------------------------


def _input_lines(prompt: str, buffer: str, width: int) -> list[str]:
    """Return the complete visible input, wrapped without eliding it.

    The raw reader redraws after every key. Its former one-row renderer used
    an ellipsis at terminal width, which made long ordinary messages appear to
    be changed even though the underlying buffer was intact. Reserve the last
    terminal column to avoid eager wrapping, then explicitly wrap all text.
    ANSI styling is intentionally removed only in this raw redraw path: escape
    bytes are not display cells and splitting them would corrupt the prompt.
    """
    visible_prompt = _ANSI_ESCAPE_RE.sub("", str(prompt or ""))
    text = visible_prompt + str(buffer or "")
    columns = max(1, int(width) - 1)
    if not text:
        return [""]
    return [text[index:index + columns] for index in range(0, len(text), columns)]


def _cursor_cell(prompt: str, buffer: str, cursor: int, width: int,
                 line_count: int) -> tuple[int, int]:
    """Return the wrapped input row/column for a buffer cursor.

    The terminal reserves its final column, so this mirrors
    :func:`_input_lines` exactly rather than trusting terminal auto-wrap.
    """
    visible_prompt = _ANSI_ESCAPE_RE.sub("", str(prompt or ""))
    prefix = visible_prompt + str(buffer or "")[:max(0, int(cursor))]
    columns = max(1, int(width) - 1)
    row = len(prefix) // columns
    column = len(prefix) % columns
    if row >= max(1, int(line_count)):
        row = max(0, int(line_count) - 1)
        column = columns
    return row, column


def _cursor_to_input_start(state: MenuState) -> str:
    """Move from the current final input row back to its first row."""
    rows = max(1, int(getattr(state, "_drawn_input_rows", 1) or 1))
    return "\r" + (CSI + "%dA" % (rows - 1) if rows > 1 else "")


def _visible_input_lines(lines: list[str], *, cursor_row: int, height: int,
                         menu_rows: int) -> tuple[list[str], int]:
    """Keep an interactive redraw inside the terminal viewport.

    The input buffer is never shortened: :func:`_finish` writes every line on
    acceptance.  While editing, a raw console cannot move its cursor into
    scrollback to erase prior rows, so redraw only the newest rows that fit
    above the command palette.  This is deliberately a viewport policy, not
    a content cap and does not add an ellipsis that could look like model/user
    text was modified.
    """
    budget = max(1, int(height) - max(0, int(menu_rows)) - 1)
    start = max(0, min(int(cursor_row), max(0, len(lines) - budget)))
    return (lines[start:start + budget] or [""], start)


def _clear_raw_input(state: MenuState, stream) -> None:
    """Erase every currently drawn raw-input row before a fallback redraw."""
    stream.write(_cursor_to_input_start(state) + CSI + "0J")
    stream.flush()


def _paint(state: MenuState, prompt: str, stream) -> None:
    """Redraw the input line and the menu, leaving the cursor where typing is.

    ``CSI 0J`` erases from the cursor to the end of the display, so the whole
    previous menu goes away without tracking how tall it was; the menu is then
    reprinted below and the cursor walked back up onto the input line.  No
    scrollback is consumed because nothing is ever printed past the last row.
    """
    rows = state.render_rows()
    cols, height = _terminal_size()
    all_lines = _input_lines(prompt, state.buffer, cols)
    cursor_row, cursor_col = _cursor_cell(
        prompt, state.buffer, state.cursor, cols, len(all_lines),
    )
    lines, start = _visible_input_lines(
        all_lines, cursor_row=cursor_row, height=height, menu_rows=len(rows),
    )
    visible_cursor_row = max(0, cursor_row - start)
    parts = [_cursor_to_input_start(state), CSI + "0J", "\n".join(lines)]
    state._drawn_input_rows = len(lines)
    if rows:
        parts.append("\n" + "\n".join(rows))
    up = len(rows) + len(lines) - 1 - visible_cursor_row
    if up:
        parts.append(CSI + "%dA" % up)
    parts.append("\r")
    if cursor_col:
        parts.append(CSI + "%dC" % cursor_col)
    stream.write("".join(parts))
    stream.flush()


def _finish(state: MenuState, prompt: str, stream) -> None:
    """Clear the menu and leave only the accepted line on screen."""
    cols, _ = _terminal_size()
    lines = _input_lines(prompt, state.buffer, cols)
    stream.write(_cursor_to_input_start(state) + CSI + "0J" + "\n".join(lines) + "\n")
    stream.flush()


def _clear_screen(state: MenuState, stream) -> None:
    """Clear terminal presentation; retain the in-progress input buffer."""
    stream.write(CSI + "2J" + CSI + "H")
    stream.flush()
    state._drawn_input_rows = 1


def _read_line_raw(prompt: str, completer=None, history=None) -> str:
    msvcrt = _msvcrt()
    stream = sys.stdout
    state = MenuState(completer=completer)
    recalled = HistoryCursor(history)
    try:
        _paint(state, prompt, stream)
        while True:
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                # Windows delivers arrows as a prefix byte plus a scan code.
                second = msvcrt.getwch()
                key = {
                    "H": KEY_UP, "P": KEY_DOWN, "K": KEY_LEFT,
                    "M": KEY_RIGHT, "G": KEY_HOME, "O": KEY_END,
                    "S": KEY_DELETE,
                }.get(second)
                if key is None:
                    continue
                # A slash prefix alone is not enough to reserve arrows: paths
                # and other ordinary slash-looking prose can have no palette
                # matches and should retain normal terminal history recall.
                if state.menu_active and state.has_palette_matches():
                    action = state.handle_key(key)
                elif key == KEY_UP:
                    state.buffer = recalled.up(state.buffer)
                    state.cursor = len(state.buffer)
                    state.dismissed = False
                    state._reset_selection()
                    action = CONTINUE
                elif key == KEY_DOWN:
                    state.buffer = recalled.down(state.buffer)
                    state.cursor = len(state.buffer)
                    state.dismissed = False
                    state._reset_selection()
                    action = CONTINUE
                else:
                    action = state.handle_key(key)
            else:
                action = state.handle_key(ch)
            if action == ACCEPT:
                _finish(state, prompt, stream)
                return state.buffer
            if action == INTERRUPT:
                _finish(state, prompt, stream)
                raise KeyboardInterrupt
            if action == CLEAR:
                _clear_screen(state, stream)
            _paint(state, prompt, stream)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        # The raw path is allowed to fail, but it must clear every wrapped
        # line before read_line's ordinary input() fallback draws a prompt.
        try:
            _clear_raw_input(state, stream)
        except Exception:
            pass
        raise


def read_line(prompt: str = "", *, enabled: bool = True, history=None) -> str:
    """Read one line, showing a live command menu while it starts with ``/``.

    Falls back to builtin :func:`input` whenever the menu cannot or should not
    run -- including when the raw path throws for any reason at all.  A broken
    menu degrades to an ordinary prompt; it never takes the REPL with it.
    ``KeyboardInterrupt`` is deliberately not caught, so Ctrl+C behaves exactly
    as it does under :func:`input`.
    """
    if not enabled or not available():
        return input(prompt)
    try:
        return _read_line_raw(prompt, history=history)
    except KeyboardInterrupt:
        raise
    except EOFError:
        raise
    except Exception:
        # Leave a clean line behind before handing over, or the fallback
        # prompt prints on top of a half-drawn menu.
        try:
            sys.stdout.write("\r" + CSI + "0J")
            sys.stdout.flush()
        except Exception:
            pass
        return input(prompt)
