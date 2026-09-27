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
import importlib
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
# Shift+Tab. Windows consoles deliver it through msvcrt.getwch() as a prefix
# byte (``\x00`` or ``\xe0``) followed by scan code 0x0F; VT input mode
# delivers ``ESC [ Z``. Both become this token.
KEY_MODE_CYCLE = "<mode-cycle>"

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
# Bracketed paste (xterm ``CSI ?2004h``): the terminal wraps pasted text in
# these markers, so a pasted newline is text, never Enter.
BRACKETED_PASTE_ON = CSI + "?2004h"
BRACKETED_PASTE_OFF = CSI + "?2004l"
PASTE_START = "[200~"
PASTE_END = CSI + "201~"
PASTE_LIMIT = 256 * 1024
_SHIFT_TAB_SCAN = "\x0f"
_WINDOWS_KEY_PREFIXES = ("\x00", "\xe0")
_WINDOWS_SCAN_KEYS = {
    "H": KEY_UP, "P": KEY_DOWN, "K": KEY_LEFT,
    "M": KEY_RIGHT, "G": KEY_HOME, "O": KEY_END,
    "S": KEY_DELETE, _SHIFT_TAB_SCAN: KEY_MODE_CYCLE,
}
_VT_CSI_KEYS = {
    "[A": KEY_UP, "[B": KEY_DOWN, "[C": KEY_RIGHT, "[D": KEY_LEFT,
    "[H": KEY_HOME, "[F": KEY_END, "[3~": KEY_DELETE, "[Z": KEY_MODE_CYCLE,
}
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SS3_KEYS = {
    "A": KEY_UP, "B": KEY_DOWN, "C": KEY_RIGHT, "D": KEY_LEFT,
    "H": KEY_HOME, "F": KEY_END,
}
# Zero-width (non-)joiners are format characters that real text needs
# (emoji sequences, Persian and Indic scripts); every other format character
# (bidi overrides and isolates, zero-width space, BOM) is never inserted.
_TEXT_FORMAT_CHARS = frozenset(("\u200c", "\u200d"))


def _insertable(ch: str) -> bool:
    """Whether one typed or pasted code point may enter the buffer.

    Controls (C0, DEL, C1 such as the 8-bit CSI ``\x9b``), surrogates and
    format characters other than ZWJ/ZWNJ are refused, so the echoed buffer
    can never carry a terminal sequence or reorder the prompt line.
    """
    category = unicodedata.category(ch)
    if category in ("Cc", "Cs"):
        return False
    if category == "Cf":
        return ch in _TEXT_FORMAT_CHARS
    return True


# --- the pure state machine ----------------------------------------------


def _load_command_catalog():
    """Load the packaged catalog only when completion is requested."""
    return importlib.import_module("sonder_runtime.adapters.command_catalog")


def _default_completer(prefix, limit=MAX_ROWS):
    """Ask the catalog what matches; never let its import cost the REPL."""
    try:
        command_catalog = _load_command_catalog()
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
        command_catalog = _load_command_catalog()
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
                 hint_provider=None, argument_completer=None, frame: str = "",
                 frame_style: str = ""):
        self.completer = completer or _default_completer
        self.hint_provider = hint_provider or _default_argument_hint
        # Argument candidates are deliberately opt-in. Most commands accept
        # free-form paths or prose, where a palette would hide the useful
        # usage hint and make Tab surprising. Callers can opt in for bounded
        # vocabularies such as the live /model choices.
        self.argument_completer = argument_completer
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
        self._argument_cache_key = None
        self._argument_cache: list = []
        # The raw reader renders before processing the next key.  Retain the
        # current height-derived selection ceiling so Tab and arrows cannot
        # refer to an entry that was clipped off the visible palette.
        self._visible_row_limit = self.limit
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

    def _argument_query(self):
        """Return ``(command, prefix, start, end)`` for one editable argument.

        Completion is intentionally limited to the first whitespace-delimited
        argument. That keeps it safe for commands whose later text is a path,
        a prompt, or another free-form value, while still allowing a cursor in
        the middle of a selected argument to replace just that argument.
        """
        text = self.buffer
        cursor = max(0, min(len(text), self.cursor))
        head = text[:cursor]
        match = re.match(r"^(\/[^\s]+)(\s+)([^\s]*)$", head)
        if match is None:
            return None
        command = match.group(1)
        prefix = match.group(3)
        start = len(match.group(1)) + len(match.group(2))
        end = cursor
        while end < len(text) and not text[end].isspace():
            end += 1
        # A second argument means this is no longer a bounded completion slot.
        if text[end:].strip():
            return None
        return command, prefix, start, end

    def argument_matches(self) -> list:
        """Return optional candidates for the current bounded argument slot."""
        query = self._argument_query()
        if query is None or self.argument_completer is None:
            return []
        command, prefix, _start, _end = query
        key = (command, prefix)
        if self._argument_cache_key != key:
            try:
                found = list(self.argument_completer(command, prefix, limit=self.limit))
            except Exception:
                found = []
            self._argument_cache_key = key
            self._argument_cache = found[: self.limit]
        return self._argument_cache

    def _selection_matches(self) -> list:
        return self.argument_matches() if self.argument_context else self.matches()

    def _visible_selection_matches(self) -> list:
        return self._selection_matches()[:self._visible_row_limit]

    def selection(self):
        """The highlighted entry, or None when nothing is highlighted."""
        rows = self._visible_selection_matches()
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
        return bool(self._visible_selection_matches())

    # -- transitions ------------------------------------------------------

    def _reset_selection(self) -> None:
        self.selected = 0
        self._visible_row_limit = self.limit

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
            rows = self._visible_selection_matches() if self.menu_active else []
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
                    if self.argument_context:
                        query = self._argument_query()
                        if query is not None:
                            _command, _prefix, start, end = query
                            value = _name_of(entry)
                            self.buffer = self.buffer[:start] + value + self.buffer[end:]
                            self.cursor = start + len(value)
                    else:
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
        if len(ch) == 1 and _insertable(ch):
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

    def insert_text(self, text: str) -> None:
        """Insert pasted text at the cursor as literal text, never as keys.

        Line breaks are kept as ``\n`` (the accepted line is one message);
        tabs become a space and every other control character is dropped, so
        a paste can neither submit the line nor inject a terminal sequence.
        """
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        clean = "".join(
            ch if ch == "\n" else (" " if ch == "\t" else ch)
            for ch in raw
            if ch in ("\n", "\t") or _insertable(ch)
        )
        if not clean:
            return
        self.buffer = self.buffer[:self.cursor] + clean + self.buffer[self.cursor:]
        self.cursor += len(clean)
        self.dismissed = False
        self._reset_selection()

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
                argument_completer=self.argument_completer, frame=self.frame,
            )
            probe.selected = self.selected
            return probe.render_rows(width=width, height=height)
        if not self.menu_active:
            return []
        if self.argument_context:
            entries = self.argument_matches()
            if entries:
                cols, lines = _terminal_size()
                width = int(width) if width else cols
                height = int(height) if height else lines
                budget = max(0, min(self.limit, MAX_ROWS, height - 2))
                self._visible_row_limit = budget
                entries = entries[:budget]
                if not entries:
                    return []
                namewidth = max(len(_name_of(entry)) for entry in entries)
                picked = max(0, min(self.selected, len(entries) - 1))
                return [_truncate("%s %-*s  %s" % (
                    ">" if index == picked else " ", namewidth,
                    _name_of(entry), _summary_of(entry),
                ), width) for index, entry in enumerate(entries)]
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
        self._visible_row_limit = budget
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


def _cell_width(character: str) -> int:
    """Return the terminal-cell width of one printable character.

    ``len`` is not a layout measurement in a terminal: combining marks occupy
    no cells and common CJK/emoji characters occupy two.  The raw composer
    needs a conservative stdlib-only approximation so a pasted project name
    or natural-language prompt cannot push a border into the next row.
    """
    if not character or unicodedata.combining(character):
        return 0
    # Variation selectors modify their preceding glyph rather than consuming
    # a separate terminal cell.
    if "VARIATION SELECTOR" in unicodedata.name(character, ""):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def _grapheme_clusters(text: str):
    """Yield a small, terminal-focused subset of Unicode grapheme clusters.

    The stdlib has no ``\\X`` grapheme iterator, but terminal input needs the
    common emoji cases to remain indivisible: combining marks/selectors,
    skin-tone modifiers, regional-indicator flags, keycaps, and ZWJ sequences.
    Keeping each sequence together prevents cursor/wrap geometry from counting
    several code points as several rendered glyphs.
    """
    value = str(text or "")
    index = 0
    while index < len(value):
        start = index
        first = value[index]
        index += 1
        regional = 0x1F1E6 <= ord(first) <= 0x1F1FF
        if regional and index < len(value) and 0x1F1E6 <= ord(value[index]) <= 0x1F1FF:
            index += 1
        while index < len(value):
            character = value[index]
            codepoint = ord(character)
            if (
                unicodedata.combining(character)
                or 0xFE00 <= codepoint <= 0xFE0F
                or 0x1F3FB <= codepoint <= 0x1F3FF
                or codepoint == 0x20E3
            ):
                index += 1
                continue
            if character == "\u200d" and index + 1 < len(value):
                # Include the joiner and the following base glyph, then keep
                # collecting that glyph's modifiers/selectors.
                index += 2
                continue
            break
        yield value[start:index]


def _cluster_width(cluster: str) -> int:
    """Return one terminal-cell measurement for a complete grapheme cluster."""
    if not cluster:
        return 0
    codepoints = [ord(character) for character in cluster]
    # Emoji presentation selectors, modifiers, keycaps, flags and joined emoji
    # each render as a single two-cell glyph on conventional terminals.
    if (
        "\u200d" in cluster
        or 0xFE0F in codepoints
        or any(0x1F3FB <= codepoint <= 0x1F3FF for codepoint in codepoints)
        or 0x20E3 in codepoints
        or sum(0x1F1E6 <= codepoint <= 0x1F1FF for codepoint in codepoints) >= 2
    ):
        return 2
    return max((_cell_width(character) for character in cluster), default=0)


def _display_width(text: str) -> int:
    """Return the terminal-cell width of plain text (without ANSI escapes)."""
    clean = _ANSI_ESCAPE_RE.sub("", str(text or ""))
    return sum(_cluster_width(cluster) for cluster in _grapheme_clusters(clean))


def _clip_cells(text: str, width: int) -> str:
    """Keep the longest prefix that fits in ``width`` terminal cells."""
    limit = max(0, int(width))
    used = 0
    kept: list[str] = []
    for cluster in _grapheme_clusters(text):
        cells = _cluster_width(cluster)
        if cells and used + cells > limit:
            break
        kept.append(cluster)
        used += cells
    return "".join(kept)


def _pad_cells(text: str, width: int, fill: str = " ") -> str:
    """Clip then pad text to exactly ``width`` terminal cells."""
    clipped = _clip_cells(text, width)
    return clipped + fill * max(0, int(width) - _display_width(clipped))


def _wrap_cells(text: str, width: int) -> list[str]:
    """Wrap text on terminal-cell boundaries without modifying the buffer."""
    limit = max(1, int(width))
    rows: list[str] = []
    row: list[str] = []
    used = 0
    for cluster in _grapheme_clusters(text):
        if cluster == "\n":
            # A pasted line break is a hard row break, never a drawn byte.
            rows.append("".join(row))
            row = []
            used = 0
            continue
        cells = _cluster_width(cluster)
        if cells and used + cells > limit and row:
            rows.append("".join(row))
            row = []
            used = 0
        row.append(cluster)
        used += cells
    if row or not rows or text.endswith("\n"):
        rows.append("".join(row))
    return rows


def _truncate(text: str, width: int) -> str:
    # width - 1: writing into the final column makes some terminals wrap
    # eagerly, which corrupts the redraw exactly like an over-long row does.
    limit = max(1, int(width) - 1)
    if _display_width(text) <= limit:
        return text
    return _clip_cells(text, max(0, limit - 1)) + "…"


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


def _enable_windows_vt_output() -> bool:
    """Enable ANSI output on a Windows console when the host supports it.

    The composer already uses ANSI cursor controls, but a process launched by
    an older console host can inherit ``ENABLE_VIRTUAL_TERMINAL_PROCESSING``
    turned off.  In that state ``CSI 3J`` is printed literally or ignored, so
    Ctrl+L and ``/clear`` cannot discard scrollback.  Enabling the flag is a
    best-effort presentation setup: redirected output and non-console handles
    simply retain their existing behaviour.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not handle or not ctypes.windll.kernel32.GetConsoleMode(
            handle, ctypes.byref(mode),
        ):
            return False
        enabled = int(mode.value) | 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if enabled != mode.value and not ctypes.windll.kernel32.SetConsoleMode(
            handle, enabled,
        ):
            return False
        return True
    except Exception:
        return False


def enable_vt() -> bool:
    """True when the terminal will interpret VT/ANSI sequences.

    POSIX terminals always do (``True``). On Windows this turns on
    ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` for stdout and returns whether that
    worked; ``False`` (legacy conhost, redirected handle, any error) means the
    caller should use no colour, no OSC 8 links and ASCII glyphs. It never
    raises.
    """
    if os.name != "nt":
        return True
    try:
        return bool(_enable_windows_vt_output())
    except Exception:
        return False


def clear_terminal_presentation(stream) -> None:
    """Discard scrollback and clear the visible terminal without input state.

    ``CSI 3J`` is understood by Windows Terminal and other VT-compatible
    hosts.  The capability enablement above covers Windows console processes
    that did not inherit VT output mode, while remaining a harmless no-op on
    hosts where ANSI is already enabled or unsupported.
    """
    _enable_windows_vt_output()
    stream.write(CSI + "3J" + CSI + "2J" + CSI + "H")
    stream.flush()


# --- availability ---------------------------------------------------------


def _msvcrt():
    import msvcrt  # noqa: F401  (import is the probe)
    return msvcrt


def available() -> bool:
    """True when a live menu can actually be drawn on this terminal.

    False for every reason a raw read would be wrong -- piped or redirected
    stdin (how the test suite and sonder_client drive the REPL), a non-Windows
    interpreter with no ``msvcrt``, or a dumb terminal -- so the caller
    silently gets plain :func:`input` instead of a broken prompt.  ``NO_COLOR``
    is intentionally *not* a reason to opt out: it requests unstyled output,
    not the loss of keyboard editing, history, or completion.
    """
    try:
        if os.environ.get("SONDER_NO_MENU"):
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


def _msvcrt_importable() -> bool:
    try:
        _msvcrt()
        return True
    except Exception:
        return False


# The raw Windows composer (the only reader with a key map) handles Shift+Tab.
# POSIX input() / readline cannot bind a key to a Python callback, so the hint
# must say "/mode to switch" there. Show a Shift+Tab hint only when
# ``supports_mode_cycle and available()``.
supports_mode_cycle = _msvcrt_importable()


def cycle_permission_mode() -> str:
    """Advance the permission mode one step through the existing mode service.

    Wrapped in ``attended_mode_change``: a key press at the console is a
    person who is present, the same authority ``/mode`` has.
    """
    from sonder_runtime.adapters.security.permission_policy import (
        permission_policy,
    )

    with permission_policy.attended_mode_change():
        return str(permission_policy.cycle_mode(1))


# --- the raw reader -------------------------------------------------------


# Kinds returned by :func:`read_key`.
KIND_KEY = "key"
KIND_PASTE = "paste"
KIND_IGNORE = "ignore"


def _read_paste(getwch, kbhit=None) -> str:
    """Collect pasted text up to the ``ESC [201~`` end marker.

    Past :data:`PASTE_LIMIT` the rest of the paste is read and discarded up
    to the end marker (or until no input is pending), so the overflow is
    never replayed as keys: a pasted line break must not submit the line.
    """
    chunk: list[str] = []
    tail = ""
    size = 0
    while True:
        ch = getwch()
        tail = (tail + ch)[-len(PASTE_END):]
        if size < PASTE_LIMIT:
            chunk.append(ch)
            size += 1
        if tail == PASTE_END:
            if size < PASTE_LIMIT or chunk[-len(PASTE_END):] == list(PASTE_END):
                del chunk[-len(PASTE_END):]
            return "".join(chunk)
        if size >= PASTE_LIMIT and (kbhit is None or not _pending(kbhit)):
            return "".join(chunk)


def read_key(getwch, kbhit) -> tuple[str, str]:
    """Read one logical key from a Windows console. Pure over its two probes.

    ``getwch``/``kbhit`` are ``msvcrt``'s (or fakes in tests). Returns
    ``(KIND_KEY, token_or_char)``, ``(KIND_PASTE, text)`` or
    ``(KIND_IGNORE, "")``. Mappings:

    * ``\x00``/``\xe0`` + scan code: arrows, Home/End/Delete, and
      ``0x0F`` = Shift+Tab -> :data:`KEY_MODE_CYCLE`;
    * ``ESC [200~ ... ESC [201~`` (bracketed paste) -> ``KIND_PASTE``;
    * ``ESC [Z`` and the VT arrow sequences (VT input mode);
    * a lone ``ESC`` (nothing pending) is Esc.
    """
    ch = getwch()
    if ch in _WINDOWS_KEY_PREFIXES:
        second = getwch()
        key = _WINDOWS_SCAN_KEYS.get(second)
        return (KIND_KEY, key) if key is not None else (KIND_IGNORE, "")
    if ch != _ESC:
        return KIND_KEY, ch
    if not _pending(kbhit):
        return KIND_KEY, _ESC
    first = getwch()
    if first == "O" and _pending(kbhit):
        # SS3 cursor keys (application cursor mode): ESC O A..D/H/F.
        key = _SS3_KEYS.get(getwch())
        return (KIND_KEY, key) if key is not None else (KIND_IGNORE, "")
    if first != "[":
        # Alt+key or an unknown sequence: never type the ESC as text.
        return KIND_IGNORE, ""
    seq = first
    while len(seq) < 16:
        nxt = getwch()
        seq += nxt
        if "@" <= nxt <= "~":
            break
    if seq == PASTE_START:
        return KIND_PASTE, _read_paste(getwch, kbhit)
    key = _VT_CSI_KEYS.get(seq)
    return (KIND_KEY, key) if key is not None else (KIND_IGNORE, "")


def _pending(kbhit) -> bool:
    try:
        return bool(kbhit())
    except Exception:
        return False


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


def _input_lines_by_cells(text: str, columns: int) -> list[str]:
    """Wrap raw input by terminal cells without splitting combining glyphs."""
    limit = max(1, int(columns))
    lines: list[str] = []
    current: list[str] = []
    used = 0
    for ch in str(text or ""):
        if ch == "\n":
            lines.append("".join(current))
            current = []
            used = 0
            continue
        cells = _cell_width(ch)
        # A zero-width mark must remain with the rendered character before it
        # even at a wrap boundary.  It has no cursor cell of its own.
        if cells and current and used + cells > limit:
            lines.append("".join(current))
            current = []
            used = 0
        current.append(ch)
        used += cells
    if current or not lines or str(text or "").endswith("\n"):
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
    title = _clip_cells(" " + clean_frame + " ", max(0, outer - 4))
    top = (chars["tl"] + chars["h"] + title
           + chars["h"] * max(0, outer - 3 - _display_width(title))
           + chars["tr"])
    text = str(buffer or "")
    content = _wrap_cells(text, inner)
    rows = [chars["v"] + " > " + _pad_cells(line, inner) + chars["v"] for line in content]
    footer_text = str(footer_hint or " Enter send %s Up/Down history %s Ctrl+L clear " % (
        chars["dot"], chars["dot"],
    ))
    footer = (chars["bl"] + chars["h"]
              + _pad_cells(footer_text, max(0, outer - 4), chars["h"])
              + chars["h"] + chars["br"])
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
    prefix = str(buffer or "")[:max(0, int(cursor))]
    rows = _wrap_cells(prefix, columns)
    row = len(rows) - 1
    column = _display_width(rows[-1])
    if row >= max(1, int(line_count)):
        return max(0, int(line_count) - 1), columns
    return row, min(columns, column)


def _cursor_cell(prompt: str, buffer: str, cursor: int, width: int,
                 line_count: int) -> tuple[int, int]:
    """Return the wrapped input row/column for a buffer cursor.

    The terminal reserves its final column, so this mirrors
    :func:`_input_lines` exactly rather than trusting terminal auto-wrap.
    """
    visible_prompt = _ANSI_ESCAPE_RE.sub("", str(prompt or ""))
    prefix = visible_prompt + str(buffer or "")[:max(0, int(cursor))]
    columns = max(1, int(width) - 1)
    if "\n" in prefix:
        # Pasted line breaks: mirror _input_lines_by_cells row for row.
        rows = _input_lines_by_cells(prefix, columns)
        row = len(rows) - 1
        column = sum(_cell_width(ch) for ch in rows[-1])
        if column >= columns:
            row, column = row + 1, 0
    else:
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
    """Clear terminal presentation and scrollback; retain the input buffer.

    ``CSI 2J`` erases only the visible screen.  Most Windows terminals retain
    that erased content in their scrollback, which makes Ctrl+L look like a
    cosmetic redraw rather than a real terminal clear.  ``CSI 3J`` asks
    VT-compatible terminals to discard the scrollback first, then the usual
    screen-and-home sequence repaints the still-typed buffer.
    """
    clear_terminal_presentation(stream)
    state._drawn_input_rows = 1
    state._drawn_cursor_row = 0


def _set_bracketed_paste(stream, on: bool) -> None:
    try:
        stream.write(BRACKETED_PASTE_ON if on else BRACKETED_PASTE_OFF)
        stream.flush()
    except Exception:
        pass


def _read_line_raw(prompt: str, completer=None, history=None, frame: str = "",
                   frame_style: str = "", argument_completer=None,
                   mode_cycle=None, refresh_frame=None) -> str:
    msvcrt = _msvcrt()
    stream = sys.stdout
    state = MenuState(
        completer=completer, frame=frame, frame_style=frame_style,
        argument_completer=argument_completer,
    )
    recalled = HistoryCursor(history)
    kbhit = getattr(msvcrt, "kbhit", None) or (lambda: False)
    paste_mode = enable_vt()
    if paste_mode:
        _set_bracketed_paste(stream, True)
    pasted_cr = False
    try:
        _paint(state, prompt, stream)
        while True:
            kind, ch = read_key(msvcrt.getwch, kbhit)
            after_pasted_cr, pasted_cr = pasted_cr, False
            if after_pasted_cr and ch == "\n":
                continue  # the LF of a pasted CRLF: one line break, not two
            if kind == KIND_IGNORE:
                continue
            if kind == KIND_PASTE:
                state.insert_text(ch)
                recalled.reset_search()
                _paint(state, prompt, stream)
                continue
            if ch == KEY_MODE_CYCLE:
                try:
                    (mode_cycle or cycle_permission_mode)()
                    if refresh_frame is not None:
                        state.frame = str(refresh_frame() or state.frame)
                except Exception:
                    pass  # a mode-service fault must not cost the prompt
                _paint(state, prompt, stream)
                continue
            if ch in _ENTER and state.buffer and _pending(kbhit):
                # Consoles without bracketed paste deliver a multi-line paste
                # as keys; an Enter with more input already queued behind it
                # is a pasted line break, not a submit.
                state.insert_text("\n")
                pasted_cr = ch == "\r"
                _paint(state, prompt, stream)
                continue
            if ch == _CTRL_R:
                state.buffer = recalled.reverse_search(state.buffer)
                state.cursor = len(state.buffer)
                state.dismissed = False
                state._reset_selection()
                _paint(state, prompt, stream)
                continue
            if ch in (KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_HOME,
                      KEY_END, KEY_DELETE):
                # read_key already translated the prefix byte + scan code.
                key = ch
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
            if action in (ACCEPT, INTERRUPT):
                # Leave paste mode before the accepted line is written, so the
                # line is the last thing on screen.
                if paste_mode:
                    _set_bracketed_paste(stream, False)
                    paste_mode = False
                _finish(state, prompt, stream)
                if action == INTERRUPT:
                    raise KeyboardInterrupt
                return state.buffer
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
    finally:
        if paste_mode:
            _set_bracketed_paste(stream, False)


def read_line(prompt: str = "", *, enabled: bool = True, history=None,
              frame: str = "", frame_style: str = "", argument_completer=None,
              fallback_prompt: str | None = None, mode_cycle=None,
              refresh_frame=None) -> str:
    """Read one line, showing a live command menu while it starts with ``/``.

    Falls back to builtin :func:`input` whenever the menu cannot or should not
    run -- including when the raw path throws for any reason at all.  A broken
    menu degrades to an ordinary prompt; it never takes the REPL with it.
    ``KeyboardInterrupt`` is deliberately not caught, so Ctrl+C behaves exactly
    as it does under :func:`input`.

    Shift+Tab calls ``mode_cycle()`` (default :func:`cycle_permission_mode`)
    and then, when given, ``refresh_frame()`` for the new composer title, and
    keeps editing the same line.
    """
    fallback = prompt if fallback_prompt is None else str(fallback_prompt)
    if not enabled or not available():
        return input(fallback)
    try:
        return _read_line_raw(prompt, history=history, frame=frame,
                              frame_style=frame_style,
                              argument_completer=argument_completer,
                              mode_cycle=mode_cycle,
                              refresh_frame=refresh_frame)
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
