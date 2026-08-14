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
import unicodedata

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
_CTRL_R = "\x12"

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
                 hint_provider=None, frame: str = "", frame_style: str = ""):
        self.completer = completer or _default_completer
        self.hint_provider = hint_provider or _default_argument_hint
        # ``frame`` is presentation only.  The input state stays exactly the
        # same, so a terminal that cannot draw chrome still gets the ordinary
        # prompt and the full, unmodified buffer.
        self.frame = str(frame or "")
        # ANSI presentation for the whole composer surface.  It is deliberately
        # separate from ``frame`` so escape codes never become part of the
        # prompt/title geometry or the accepted user input.
        self.frame_style = str(frame_style or "")
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
        self._drawn_cursor_row = 0

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
                hint_provider=self.hint_provider, frame=self.frame,
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
        self._search_term = None
        self._search_index = len(self.entries)

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
        self.reset_search()

    def reset_search(self) -> None:
        self._search_term = None
        self._search_index = len(self.entries)

    def reverse_search(self, current: str) -> str:
        """Recall the next older history entry containing the initial query.

        Ctrl+R is deliberately in-memory only.  Repeated presses retain the
        initial search term rather than treating a recalled full command as a
        new query, matching the shell behavior users expect.
        """
        if not self.entries:
            return current
        if self._search_term is None:
            self._search_term = str(current or "").casefold()
            self._search_index = len(self.entries)
        for index in range(self._search_index - 1, -1, -1):
            if self._search_term in self.entries[index].casefold():
                self._search_index = index
                return self.entries[index]
        return current


def _truncate(text: str, width: int) -> str:
    # width - 1: writing into the final column makes some terminals wrap
    # eagerly, which corrupts the redraw exactly like an over-long row does.
    limit = max(1, int(width) - 1)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _windows_console_size():
    """Return the visible Windows console viewport, when it is available.

    ``shutil.get_terminal_size`` correctly honors the portable ``COLUMNS``
    convention, but Windows Terminal can leave it at its conservative 80-column
    fallback even when the maximized viewport is much wider.  The raw composer
    draws directly to that viewport, so prefer the console's own dimensions.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes

        class _Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class _Rect(ctypes.Structure):
            _fields_ = [
                ("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short),
            ]

        class _ConsoleScreenBufferInfo(ctypes.Structure):
            _fields_ = [
                ("dwSize", _Coord), ("dwCursorPosition", _Coord),
                ("wAttributes", ctypes.c_ushort), ("srWindow", _Rect),
                ("dwMaximumWindowSize", _Coord),
            ]

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        info = _ConsoleScreenBufferInfo()
        if not handle or not ctypes.windll.kernel32.GetConsoleScreenBufferInfo(
            handle, ctypes.byref(info),
        ):
            return None
        columns = int(info.srWindow.Right - info.srWindow.Left + 1)
        lines = int(info.srWindow.Bottom - info.srWindow.Top + 1)
        return (columns, lines) if columns > 0 and lines > 0 else None
    except Exception:
        return None


def _terminal_size():
    try:
        native = _windows_console_size()
        if native is not None:
            return max(20, native[0]), max(4, native[1])
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


def _cell_width(ch: str) -> int:
    """Return the terminal-cell width of one Unicode code point.

    The raw, unframed composer cannot delegate wrapping to the console: doing
    so makes its redraw bookkeeping depend on terminal-specific eager-wrap
    behaviour.  Python string indexes are code points, though, while a CJK
    character or ordinary emoji consumes two terminal cells and combining
    marks consume none.  Keep the conservative width rule here, where it is
    used only by the unframed renderer; framed rendering deliberately retains
    its existing fixed-character geometry.
    """
    if not ch or unicodedata.combining(ch) or ch in ("\u200c", "\u200d"):
        return 0
    # Variation selectors and emoji skin-tone modifiers modify the preceding
    # glyph rather than occupying a cursor cell of their own.
    codepoint = ord(ch)
    if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
        return 0
    if 0x1F3FB <= codepoint <= 0x1F3FF:
        return 0
    if unicodedata.category(ch).startswith("C"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1


def _display_width(text: str) -> int:
    """Return the approximate number of terminal cells used by *text*."""
    return sum(_cell_width(ch) for ch in str(text or ""))


def _input_lines_by_cells(text: str, columns: int) -> list[str]:
    """Wrap raw input by terminal cells without splitting combining glyphs."""
    limit = max(1, int(columns))
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for ch in str(text or ""):
        cells = _cell_width(ch)
        # A zero-width mark must remain with the rendered character before it
        # even at a wrap boundary.  It has no cursor cell of its own.
        if cells and current and used + cells > limit:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(ch)
        used += cells
    if current or not lines:
        lines.append("".join(current))
    return lines


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
    return _input_lines_by_cells(text, columns)


def _frame_chars(stream) -> dict[str, str]:
    """Use Unicode chrome when the active console can encode it."""
    try:
        "╭╮╰╯│─·".encode(getattr(stream, "encoding", None) or "utf-8")
        return {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "v": "│", "h": "─", "dot": "·"}
    except (LookupError, UnicodeEncodeError):
        return {"tl": "+", "tr": "+", "bl": "+", "br": "+", "v": "|", "h": "-", "dot": "."}


def _framed_input_lines(frame: str, buffer: str, width: int, stream,
                        footer_hint: str = "") -> tuple[str, list[str], str]:
    """Build a compact composer title, full-width editable rows, and footer.

    The content buffer itself is never shortened. Only the fixed title/footer
    labels are clipped to make a reliable terminal rectangle.
    """
    outer = max(8, int(width) - 1)
    # Reserve a small prompt marker inside the active chat surface.
    inner = max(1, outer - 5)
    clean_frame = _ANSI_ESCAPE_RE.sub("", str(frame or "")).strip()
    chars = _frame_chars(stream)
    title = (" " + clean_frame + " ")[: max(0, outer - 4)]
    top = chars["tl"] + chars["h"] + title + chars["h"] * max(0, outer - 2 - len(title) - 1) + chars["tr"]
    text = str(buffer or "")
    content = [text[index:index + inner] for index in range(0, len(text), inner)] or [""]
    rows = [chars["v"] + " > " + line.ljust(inner) + chars["v"] for line in content]
    footer_text = str(footer_hint or " Enter send %s Up/Down history %s Ctrl+L clear " % (
        chars["dot"], chars["dot"],
    ))
    footer = chars["bl"] + chars["h"] + footer_text[: max(0, outer - 4)].ljust(max(0, outer - 4), chars["h"]) + chars["h"] + chars["br"]
    return top, rows, footer


def _composer_footer(state: MenuState, stream) -> str:
    """Return terse controls that match the current interaction mode."""
    dot = _frame_chars(stream)["dot"]
    if state.menu_active and state.has_palette_matches():
        return " Tab complete %s Up/Down select %s Esc dismiss " % (dot, dot)
    if state.argument_context:
        return " Enter run %s Up/Down history %s Ctrl+L clear " % (dot, dot)
    return " Enter send %s Ctrl+R search %s Ctrl+L clear " % (dot, dot)


def _framed_cursor_cell(buffer: str, cursor: int, width: int,
                        line_count: int) -> tuple[int, int]:
    outer = max(8, int(width) - 1)
    columns = max(1, outer - 5)
    offset = max(0, min(len(str(buffer or "")), int(cursor)))
    row = offset // columns
    if row >= max(1, int(line_count)):
        return max(0, int(line_count) - 1), columns
    return row, min(columns, offset % columns)


def _cursor_cell(prompt: str, buffer: str, cursor: int, width: int,
                 line_count: int) -> tuple[int, int]:
    """Return the wrapped input row/column for a buffer cursor.

    The terminal reserves its final column, so this mirrors
    :func:`_input_lines` exactly rather than trusting terminal auto-wrap.
    """
    visible_prompt = _ANSI_ESCAPE_RE.sub("", str(prompt or ""))
    prefix = visible_prompt + str(buffer or "")[:max(0, int(cursor))]
    columns = max(1, int(width) - 1)
    cells = _display_width(prefix)
    row = cells // columns
    column = cells % columns
    if row >= max(1, int(line_count)):
        row = max(0, int(line_count) - 1)
        column = columns
    return row, column


def _cursor_to_input_start(state: MenuState) -> str:
    """Move from the current cursor cell back to the first rendered row."""
    row = max(0, int(getattr(state, "_drawn_cursor_row", 0) or 0))
    return "\r" + (CSI + "%dA" % row if row else "")


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


def _styled_frame(text: str, style: str) -> str:
    """Apply optional terminal-only styling without changing frame geometry."""
    if not style:
        return text
    return style + text + "\x1b[0m"


def _paint(state: MenuState, prompt: str, stream) -> None:
    """Redraw the input line and the menu, leaving the cursor where typing is.

    ``CSI 0J`` erases from the cursor to the end of the display, so the whole
    previous menu goes away without tracking how tall it was; the menu is then
    reprinted below and the cursor walked back up onto the input line.  No
    scrollback is consumed because nothing is ever printed past the last row.
    """
    rows = state.render_rows()
    cols, height = _terminal_size()
    framed = bool(state.frame)
    if framed:
        top, all_lines, footer = _framed_input_lines(
            state.frame, state.buffer, cols, stream,
            footer_hint=_composer_footer(state, stream))
        cursor_row, cursor_col = _framed_cursor_cell(
            state.buffer, state.cursor, cols, len(all_lines))
        lines, start = _visible_input_lines(
            all_lines, cursor_row=cursor_row, height=max(1, height - 2),
            menu_rows=len(rows),
        )
    else:
        all_lines = _input_lines(prompt, state.buffer, cols)
        cursor_row, cursor_col = _cursor_cell(
            prompt, state.buffer, state.cursor, cols, len(all_lines),
        )
        lines, start = _visible_input_lines(
            all_lines, cursor_row=cursor_row, height=height, menu_rows=len(rows),
        )
    visible_cursor_row = max(0, cursor_row - start)
    if framed:
        body = "\n".join(_styled_frame(row, state.frame_style) for row in (
            [top] + lines + [footer]
        ))
        input_rows = len(lines) + 2
        cursor_from_start = visible_cursor_row + 1
        cursor_col += 4  # border, breathing space, and prompt marker
    else:
        body = "\n".join(lines)
        input_rows = len(lines)
        cursor_from_start = visible_cursor_row
    # Command choices belong immediately above the composer, like a chat
    # autocomplete panel. Keeping them below the box made the footer read as
    # a separator instead of the bottom edge of the active input surface.
    prefix = ("\n".join(rows) + "\n") if framed and rows else ""
    parts = [_cursor_to_input_start(state), CSI + "0J", prefix, body]
    state._drawn_input_rows = input_rows
    state._drawn_cursor_row = cursor_from_start + (len(rows) if framed else 0)
    if rows and not framed:
        parts.append("\n" + "\n".join(rows))
    up = (input_rows - 1 - cursor_from_start) if framed else (
        len(rows) + input_rows - 1 - cursor_from_start)
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
    if state.frame:
        top, lines, footer = _framed_input_lines(
            state.frame, state.buffer, cols, stream,
            footer_hint=_composer_footer(state, stream))
        rendered = "\n".join(_styled_frame(row, state.frame_style) for row in (
            [top] + lines + [footer]
        ))
    else:
        rendered = "\n".join(_input_lines(prompt, state.buffer, cols))
    stream.write(_cursor_to_input_start(state) + CSI + "0J" + rendered + "\n")
    stream.flush()


def _clear_screen(state: MenuState, stream) -> None:
    """Clear terminal presentation; retain the in-progress input buffer."""
    stream.write(CSI + "2J" + CSI + "H")
    stream.flush()
    state._drawn_input_rows = 1
    state._drawn_cursor_row = 0


def _read_line_raw(prompt: str, completer=None, history=None, frame: str = "",
                   frame_style: str = "") -> str:
    msvcrt = _msvcrt()
    stream = sys.stdout
    state = MenuState(completer=completer, frame=frame, frame_style=frame_style)
    recalled = HistoryCursor(history)
    try:
        _paint(state, prompt, stream)
        while True:
            ch = msvcrt.getwch()
            if ch == _CTRL_R:
                state.buffer = recalled.reverse_search(state.buffer)
                state.cursor = len(state.buffer)
                state.dismissed = False
                state._reset_selection()
                _paint(state, prompt, stream)
                continue
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
                    recalled.reset_search()
                    state.buffer = recalled.up(state.buffer)
                    state.cursor = len(state.buffer)
                    state.dismissed = False
                    state._reset_selection()
                    action = CONTINUE
                elif key == KEY_DOWN:
                    recalled.reset_search()
                    state.buffer = recalled.down(state.buffer)
                    state.cursor = len(state.buffer)
                    state.dismissed = False
                    state._reset_selection()
                    action = CONTINUE
                else:
                    action = state.handle_key(key)
            else:
                action = state.handle_key(ch)
                # Any edit starts a fresh reverse-search query on the next
                # Ctrl+R; entering a character must not keep an old search
                # term alive invisibly.
                if ch not in (_CTRL_R,):
                    recalled.reset_search()
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


def read_line(prompt: str = "", *, enabled: bool = True, history=None,
              frame: str = "", frame_style: str = "",
              fallback_prompt: str | None = None) -> str:
    """Read one line, showing a live command menu while it starts with ``/``.

    Falls back to builtin :func:`input` whenever the menu cannot or should not
    run -- including when the raw path throws for any reason at all.  A broken
    menu degrades to an ordinary prompt; it never takes the REPL with it.
    ``KeyboardInterrupt`` is deliberately not caught, so Ctrl+C behaves exactly
    as it does under :func:`input`.
    """
    fallback = prompt if fallback_prompt is None else str(fallback_prompt)
    if not enabled or not available():
        return input(fallback)
    try:
        return _read_line_raw(prompt, history=history, frame=frame,
                              frame_style=frame_style)
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
        return input(fallback)
