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
import shutil
import sys

# --- key tokens and actions ----------------------------------------------

# Arrow keys arrive from msvcrt.getwch() as a two-character sequence, so the
# reader translates them into these tokens before handing them to the state
# machine.  They are spelled with angle brackets so they can never collide with
# a character the user could actually type.
KEY_UP = "<up>"
KEY_DOWN = "<down>"

# handle_key's return values.
CONTINUE = "continue"   # keep reading; the caller should repaint
ACCEPT = "accept"       # the line is finished
INTERRUPT = "interrupt"  # Ctrl+C: the caller should raise KeyboardInterrupt

_ENTER = ("\r", "\n")
_BACKSPACE = ("\x08", "\x7f")
_TAB = "\t"
_ESC = "\x1b"
_CTRL_C = "\x03"
_CTRL_U = "\x15"

MAX_ROWS = 8

CSI = "\x1b["


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


class MenuState:
    """Buffer + highlight for one input line.  No terminal, no I/O.

    ``handle_key`` takes one key (a printable character, a control character,
    or one of the ``KEY_*`` tokens) and returns one of :data:`CONTINUE`,
    :data:`ACCEPT`, :data:`INTERRUPT`.  ``render_rows`` turns the current
    state into the exact lines to draw.  Everything the reader needs is in
    those two methods, which is what makes the interaction testable without a
    terminal.
    """

    def __init__(self, completer=None, limit: int = MAX_ROWS, buffer: str = ""):
        self.completer = completer or _default_completer
        self.limit = max(1, int(limit))
        self.buffer = str(buffer or "")
        self.selected = 0
        self.dismissed = False
        self._cache_key = None
        self._cache: list = []

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
        rows = self.matches()
        if not rows:
            return None
        return rows[max(0, min(self.selected, len(rows) - 1))]

    # -- transitions ------------------------------------------------------

    def _reset_selection(self) -> None:
        self.selected = 0

    def handle_key(self, ch: str) -> str:
        if ch == KEY_UP:
            if self.menu_active and self.matches():
                # Clamps rather than wrapping: wrapping from the first entry to
                # the last is a surprise when the list is being retyped under
                # you, and the top entry is the one you usually want.
                self.selected = max(0, self.selected - 1)
            return CONTINUE
        if ch == KEY_DOWN:
            rows = self.matches() if self.menu_active else []
            if rows:
                self.selected = min(len(rows) - 1, self.selected + 1)
            return CONTINUE
        if ch == _CTRL_C:
            return INTERRUPT
        if ch == _CTRL_U:
            self.buffer = ""
            self.dismissed = False
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
                    self._reset_selection()
            return CONTINUE
        if ch in _BACKSPACE:
            self.buffer = self.buffer[:-1]
            if not self.buffer:
                # An empty line is a fresh start, so a later "/" re-opens the
                # menu even after Esc.
                self.dismissed = False
            self._reset_selection()
            return CONTINUE
        if len(ch) == 1 and (ch >= " " and ch != "\x7f"):
            self.buffer += ch
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
            probe = MenuState(self.completer, self.limit, buffer=str(prefix))
            probe.selected = self.selected
            return probe.render_rows(width=width, height=height)
        if not self.menu_active:
            return []
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


def _paint(state: MenuState, prompt: str, stream) -> None:
    """Redraw the input line and the menu, leaving the cursor where typing is.

    ``CSI 0J`` erases from the cursor to the end of the display, so the whole
    previous menu goes away without tracking how tall it was; the menu is then
    reprinted below and the cursor walked back up onto the input line.  No
    scrollback is consumed because nothing is ever printed past the last row.
    """
    rows = state.render_rows()
    line = prompt + state.buffer
    cols, _ = _terminal_size()
    line_out = _truncate(line, cols) if len(line) >= cols else line
    parts = ["\r", CSI + "0J", line_out]
    if rows:
        parts.append("\n" + "\n".join(rows))
        parts.append(CSI + "%dA" % len(rows))
        parts.append("\r")
        if len(line_out):
            parts.append(CSI + "%dC" % len(line_out))
    stream.write("".join(parts))
    stream.flush()


def _finish(state: MenuState, prompt: str, stream) -> None:
    """Clear the menu and leave only the accepted line on screen."""
    line = prompt + state.buffer
    cols, _ = _terminal_size()
    stream.write("\r" + CSI + "0J" + (
        _truncate(line, cols) if len(line) >= cols else line
    ) + "\n")
    stream.flush()


def _read_line_raw(prompt: str, completer=None) -> str:
    msvcrt = _msvcrt()
    stream = sys.stdout
    state = MenuState(completer=completer)
    _paint(state, prompt, stream)
    while True:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            # Windows delivers arrows as a prefix byte plus a scan code.
            second = msvcrt.getwch()
            key = {"H": KEY_UP, "P": KEY_DOWN}.get(second)
            if key is None:
                continue
            action = state.handle_key(key)
        else:
            action = state.handle_key(ch)
        if action == ACCEPT:
            _finish(state, prompt, stream)
            return state.buffer
        if action == INTERRUPT:
            _finish(state, prompt, stream)
            raise KeyboardInterrupt
        _paint(state, prompt, stream)


def read_line(prompt: str = "", *, enabled: bool = True) -> str:
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
        return _read_line_raw(prompt)
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
