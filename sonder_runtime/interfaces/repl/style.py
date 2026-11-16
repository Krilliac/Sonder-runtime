"""Terminal style foundation for the interactive REPL.

Everything the REPL draws as chrome -- banner, status line, live line, turn
header and footer, tool calls, notices, tables, wrapped prose and the
Markdown of an answer -- is built here from three small vocabularies:

* capabilities (:func:`caps`), detected once from the environment and the
  output stream, and injectable for tests;
* semantic colour roles (``text``, ``muted``, ``accent``, ``info``,
  ``success``, ``warning``, ``danger``, ``strong``), painted by :func:`s`;
* glyph tokens with an ASCII fallback, looked up by :func:`g`.

The module is stdlib-only and imports nothing from ``sonder_runtime``: the
interfaces layer may not import the platform layer, so environment, stream
and platform facts arrive by injection or straight from ``os``/``sys``.  No
function here writes to a stream or starts a thread; callers print what they
are given.  Colour ``none`` guarantees the returned strings hold no ESC byte,
and ASCII glyphs guarantee the chrome holds no non-ASCII character.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shutil
import sys
import unicodedata
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

__all__ = [
    "Caps", "caps", "set_caps", "reset_caps",
    "ROLES", "GLYPHS", "s", "sgr", "g", "link", "strip_ansi", "cell_width",
    "cols", "rule", "wrap", "table", "safe_text", "truncate",
    "NOTICE_KINDS", "notice", "turn_header", "tool_call", "markdown",
    "StatusState", "status_line", "LiveState", "live_line",
    "FooterState", "footer", "BannerState", "banner", "about_lines",
    "mode_roles", "compact_count", "duration_label",
]

# ---------------------------------------------------------------------------
# Capabilities (spec 2.1)
# ---------------------------------------------------------------------------

COLOR_LEVELS = ("none", "16", "256", "truecolor")
THEMES = ("dark", "light", "unknown")

_LINK_TERM_PROGRAMS = frozenset(("iTerm.app", "WezTerm", "vscode"))
_CJK_LOCALE = re.compile(r"^(zh|ja|ko)([_.@-]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Caps:
    """What the output terminal can render, decided once per process.

    ``color`` is one of ``none``/``16``/``256``/``truecolor``; ``theme`` is
    ``dark``/``light``/``unknown`` (unknown renders with the ANSI-16 roles,
    which the terminal's own theme adapts); ``glyphs`` is ``unicode`` or
    ``ascii``.  ``motion`` allows animation, ``plain`` asks for word labels
    and change-only status, ``links`` allows OSC 8 hyperlinks, and
    ``ambiguous_wide`` records a CJK-width terminal where ambiguous glyphs
    such as ``·`` and ``─`` take two cells.
    """

    color: str = "none"
    theme: str = "unknown"
    glyphs: str = "ascii"
    motion: bool = False
    plain: bool = False
    links: bool = False
    ambiguous_wide: bool = False

    @property
    def palette(self) -> str:
        """The palette actually used: 16-colour roles unless the theme is known."""
        if self.color == "none":
            return "none"
        if self.theme == "unknown" or self.color == "16":
            return "16"
        return self.color


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() not in ("", "0", "false", "no", "off")


def _is_tty(stream: Any) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _theme_from_env(env: Mapping[str, str]) -> str:
    chosen = str(env.get("SONDER_THEME") or "").strip().lower()
    if chosen in ("dark", "light"):
        return chosen
    fgbg = str(env.get("COLORFGBG") or "").strip()
    if fgbg:
        background = fgbg.split(";")[-1].strip()
        if background.isdigit():
            value = int(background)
            if value in (7, 15):
                return "light"
            if 0 <= value <= 6 or value == 8:
                return "dark"
    return "unknown"


def _encodes(stream: Any, text: str) -> bool:
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return False
    return True


def _default_vt_enable(env: Mapping[str, str]) -> bool:
    """Without an injected probe, trust only terminals known to speak VT.

    The REPL integration passes the composer's real ``enable_vt`` probe;
    this conservative fallback keeps a bare call from painting escapes onto
    a legacy conhost.
    """
    return bool(env.get("WT_SESSION") or env.get("TERM_PROGRAM")
                or env.get("ANSICON") or env.get("ConEmuANSI") == "ON")


def detect_caps(
    env: Optional[Mapping[str, str]] = None,
    stream: Any = None,
    platform: Optional[str] = None,
    vt_enable: Optional[Callable[[], bool]] = None,
) -> Caps:
    """Decide capabilities from explicit inputs (no caching).

    ``env`` defaults to ``os.environ``, ``stream`` to ``sys.stdout``,
    ``platform`` to ``os.name``.  ``vt_enable`` is called at most once, and
    only on ``nt``, to switch the console into VT mode; it returns whether
    that worked.
    """
    env = os.environ if env is None else env
    stream = sys.stdout if stream is None else stream
    platform = os.name if platform is None else platform
    term = str(env.get("TERM") or "")
    dumb = term.lower() in ("dumb", "unknown")
    tty = _is_tty(stream)

    vt_ok = True
    if platform == "nt" and tty:
        try:
            vt_ok = bool(vt_enable() if vt_enable is not None
                         else _default_vt_enable(env))
        except Exception:
            vt_ok = False

    colorterm = str(env.get("COLORTERM") or "").lower()
    if env.get("NO_COLOR"):
        color = "none"
    elif _truthy(env.get("FORCE_COLOR")) or _truthy(env.get("CLICOLOR_FORCE")):
        if colorterm in ("truecolor", "24bit"):
            color = "truecolor"
        elif term.endswith("256color"):
            color = "256"
        else:
            color = "16"
    elif dumb or (term == "" and platform != "nt"):
        color = "none"
    elif not tty:
        color = "none"
    elif platform == "nt" and not vt_ok:
        color = "none"
    elif colorterm in ("truecolor", "24bit"):
        color = "truecolor"
    elif platform == "nt" and env.get("WT_SESSION"):
        # Windows Terminal draws 24-bit colour and does not set COLORTERM.
        # The palette still waits for a known theme (see ``Caps.palette``).
        color = "truecolor"
    elif term.endswith("256color"):
        color = "256"
    else:
        color = "16"

    plain = _truthy(env.get("SONDER_PLAIN")) or term.lower() == "dumb"
    ambiguous_wide = _truthy(env.get("SONDER_AMBIGUOUS_WIDE")) or bool(
        _CJK_LOCALE.match(str(env.get("LC_ALL") or env.get("LC_CTYPE")
                              or env.get("LANG") or ""))
    )
    explicit = str(env.get("SONDER_GLYPHS") or "").strip().lower()
    if explicit in ("unicode", "ascii"):
        glyphs = explicit
    elif (plain or dumb or ambiguous_wide
          or not _encodes(stream, "".join(_UNICODE_ALL.values()))
          or (platform == "nt" and (not vt_ok or not (
              env.get("WT_SESSION") or env.get("TERM_PROGRAM"))))):
        glyphs = "ascii"
    else:
        glyphs = "unicode"
    if plain and explicit != "unicode":
        glyphs = "ascii"

    links = color != "none" and not plain and (
        bool(env.get("WT_SESSION")) or bool(env.get("VTE_VERSION"))
        or str(env.get("TERM_PROGRAM") or "") in _LINK_TERM_PROGRAMS
        or "kitty" in term or term.startswith("foot")
    )
    return Caps(
        color=color,
        theme=_theme_from_env(env),
        glyphs=glyphs,
        motion=not (plain or dumb or color == "none"),
        plain=plain,
        links=links,
        ambiguous_wide=ambiguous_wide,
    )


_CACHED: Optional[Caps] = None


def caps(
    env: Optional[Mapping[str, str]] = None,
    stream: Any = None,
    platform: Optional[str] = None,
    vt_enable: Optional[Callable[[], bool]] = None,
    *,
    refresh: bool = False,
) -> Caps:
    """Process-wide capabilities, detected on first use and cached.

    Passing any of ``env``/``stream``/``platform``/``vt_enable`` computes a
    fresh result without touching the cache (tests); ``refresh=True``
    re-detects from the real process and replaces the cache (REPL start,
    after ``vt_enable`` is known).
    """
    global _CACHED
    injected = any(v is not None for v in (env, stream, platform, vt_enable))
    if injected and not refresh:
        return detect_caps(env, stream, platform, vt_enable)
    if _CACHED is None or refresh:
        _CACHED = detect_caps(env, stream, platform, vt_enable)
    return _CACHED


def set_caps(value: Caps) -> Caps:
    """Pin the process capabilities (REPL start or tests)."""
    global _CACHED
    _CACHED = value
    return value


def reset_caps() -> None:
    global _CACHED
    _CACHED = None


def _c(c: Optional[Caps]) -> Caps:
    return c if c is not None else caps()


# ---------------------------------------------------------------------------
# Semantic tokens (spec 2.2)
# ---------------------------------------------------------------------------

ROLES = ("text", "muted", "accent", "info", "success", "warning", "danger",
         "strong", "reverse")

# The accent is Iris, the blue-violet of the app (app/lib/theme.dart): bright
# blue where the terminal has 16 colours, which leaves cyan for ``info``.
_SGR16 = {
    "text": "39", "muted": "2", "accent": "94", "info": "36",
    "success": "32", "warning": "33", "danger": "31",
    "strong": "1", "reverse": "7",
}
# 256-colour cells and truecolor values, (dark, light).  The truecolor values
# are the app's text tones for the same roles (muted, accentText, info, ok,
# warn, danger in app/lib/theme.dart), so a turn reads the same in the
# terminal and the app; a test compares the two files.  Every value reaches
# 4.5:1 against its theme background (#1e1e1e and #000000 / #ffffff); tests
# compute it.
_C256 = {
    "muted": (245, 242), "accent": (147, 62), "info": (117, 24),
    "success": (114, 28), "warning": (221, 130), "danger": (210, 124),
}
_TRUE = {
    "muted": ((158, 154, 146), (102, 99, 93)),
    "accent": ((182, 176, 250), (77, 69, 181)),
    "info": ((116, 194, 230), (27, 101, 132)),
    "success": ((127, 207, 150), (25, 107, 60)),
    "warning": ((237, 191, 102), (131, 85, 0)),
    "danger": ((242, 127, 123), (181, 45, 45)),
}
_RESET = "\x1b[0m"
_ANSI_RE = re.compile(r"\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _sgr(role: str, c: Caps) -> str:
    palette = c.palette
    if role in ("text", "strong", "reverse") or palette == "16":
        return _SGR16[role]
    index = 0 if c.theme == "dark" else 1
    if palette == "256":
        return "38;5;%d" % _C256[role][index]
    return "38;2;%d;%d;%d" % _TRUE[role][index]


def s(text: Any, *roles: str, c: Optional[Caps] = None) -> str:
    """Paint ``text`` with semantic roles; plain text when colour is off.

    Inner painted spans end with a reset; the outer roles are re-applied
    after each one, so nesting keeps both styles.
    """
    c = _c(c)
    value = str(text)
    roles = tuple(r for r in roles if r)
    if c.color == "none" or not roles or not value:
        return value
    for role in roles:
        if role not in _SGR16:
            raise ValueError("unknown style role: %r" % role)
    start = "\x1b[%sm" % ";".join(_sgr(r, c) for r in roles)
    return start + value.replace(_RESET, _RESET + start) + _RESET


def sgr(*roles: str, c: Optional[Caps] = None) -> str:
    """The escape that opens ``roles``, or ``""`` when colour is off.

    For a caller that draws its own cells and only needs the tint (the raw
    composer's frame); whatever it opens must be closed with a reset.
    """
    painted = s("\x00", *roles, c=c)
    return painted.split("\x00", 1)[0] if painted != "\x00" else ""


def mode_roles(mode: str, elevated: bool = False) -> tuple:
    """Roles for a permission-mode word (spec 2.2 Modes)."""
    if elevated:
        return ("danger", "reverse")
    return {
        "plan": ("muted",),
        "manual": ("text",),
        "acceptEdits": ("warning",),
        "auto": ("warning", "strong"),
    }.get(str(mode or ""), ("text",))


def strip_ansi(text: Any) -> str:
    return _ANSI_RE.sub("", str(text))


def link(text: Any, url: str, c: Optional[Caps] = None) -> str:
    """OSC 8 hyperlink when the terminal is on the allow-list, else text."""
    c = _c(c)
    value = str(text)
    # Any C0/C1 control or format character in the URL could end the OSC
    # early (ESC, BEL, 8-bit ST/CSI/OSC) or hide text; such URLs stay plain.
    if not c.links or not url or any(
            unicodedata.category(ch) in ("Cc", "Cf") for ch in url):
        return value
    return "\x1b]8;;%s\x1b\\%s\x1b]8;;\x1b\\" % (url, value)


# ---------------------------------------------------------------------------
# Glyph tokens (spec 2.3)
# ---------------------------------------------------------------------------

_UNICODE_GLYPHS = {
    "mark": "◈", "prompt": "❯", "sep": "·", "rule": "─",
    "arrow": "→", "ellipsis": "…", "emdash": "—",
    "ok": "✓", "fail": "✗", "refused": "⊘", "warn": "!",
    "ask": "?", "tool": "▸", "skip": "–", "note": "·",
    "up": "↑",
}
_ASCII_GLYPHS = {
    "mark": "#", "prompt": ">", "sep": "|", "rule": "-", "arrow": "->",
    "ellipsis": "...", "emdash": "--", "ok": "+", "fail": "x",
    "refused": "x", "warn": "!", "ask": "?", "tool": "-", "skip": "-",
    "note": "*", "up": "Up",
}
# Layout glyphs only the terminal draws: the corner under a tool call, a
# list bullet and a quote bar, as (unicode, ascii).  They are not status
# vocabulary, so they stay out of the two tables above, which the app's
# status_vocab.dart mirrors token for token.
_LAYOUT_GLYPHS = {
    "result": ("└", "+-"), "bullet": ("•", "-"), "quote": ("│", "|"),
}
_UNICODE_ALL = dict(_UNICODE_GLYPHS, **{k: v[0] for k, v in _LAYOUT_GLYPHS.items()})
_ASCII_ALL = dict(_ASCII_GLYPHS, **{k: v[1] for k, v in _LAYOUT_GLYPHS.items()})
GLYPHS = {"unicode": dict(_UNICODE_ALL), "ascii": dict(_ASCII_ALL)}


def g(name: str, c: Optional[Caps] = None) -> str:
    """A glyph token in the active glyph set."""
    c = _c(c)
    table_ = _ASCII_ALL if c.glyphs == "ascii" else _UNICODE_ALL
    return table_[name]


def _sep(c: Caps) -> str:
    return " %s " % g("sep", c)


# ---------------------------------------------------------------------------
# Measurement and text safety
# ---------------------------------------------------------------------------

def _char_cells(ch: str) -> int:
    if not ch or unicodedata.combining(ch):
        return 0
    if 0xFE00 <= ord(ch) <= 0xFE0F or ch in "\u200c\u200d":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(text: Any) -> int:
    """Terminal cells ``text`` occupies, ignoring ANSI escapes."""
    return sum(_char_cells(ch) for ch in strip_ansi(text))


def _units(text: str):
    """Split into escape sequences (width 0) and single characters."""
    pos = 0
    for match in _ANSI_RE.finditer(text):
        for ch in text[pos:match.start()]:
            yield ch, _char_cells(ch)
        yield match.group(0), 0
        pos = match.end()
    for ch in text[pos:]:
        yield ch, _char_cells(ch)


def truncate(text: Any, width: int, c: Optional[Caps] = None) -> str:
    """Cut ``text`` to ``width`` cells, ending with the ellipsis glyph."""
    c = _c(c)
    value = str(text)
    width = max(0, int(width))
    if cell_width(value) <= width:
        return value
    mark = g("ellipsis", c)
    room = width - cell_width(mark)
    if room <= 0:
        return mark[:width]
    out, used, escaped, link_open = [], 0, False, False
    for unit, size in _units(value):
        if size == 0 and unit.startswith("\x1b"):
            out.append(unit)
            if unit.startswith("\x1b]8;"):
                # "\x1b]8;;\x1b\\" closes a link; anything longer opens one.
                link_open = not unit.startswith("\x1b]8;;\x1b") and not unit.startswith("\x1b]8;;\x07")
            else:
                escaped = True
            continue
        if used + size > room:
            break
        out.append(unit)
        used += size
    tail = _RESET if escaped and c.color != "none" else ""
    if link_open:
        # Cutting inside a hyperlink must still close it, or everything
        # printed afterwards stays clickable.
        tail += "\x1b]8;;\x1b\\"
    return "".join(out) + tail + mark


_SAFE_KEEP = frozenset("\n\t")
_UNSAFE_CATEGORIES = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))
# ZWNJ/ZWJ shape Indic and Persian script and emoji sequences; they cannot
# move the cursor or reorder text, so they are the only format chars kept.
_JOINERS = frozenset("\u200c\u200d")


def safe_text(value: Any) -> str:
    """Make untrusted text inert for a terminal, without wrapping it.

    ``\\n`` and ``\\t`` are kept.  Every other control (C0, DEL, C1),
    format character except ZWNJ/ZWJ (so bidi overrides and isolates),
    surrogate, and line/paragraph separator is shown as a
    visible ``\\xNN`` / ``\\uNNNN`` escape, so model answers, file contents
    and error text cannot move the cursor, retitle the window, write the
    clipboard (OSC 52) or reorder what the reader sees.
    """
    out = []
    for ch in str(value if value is not None else ""):
        if ch in _SAFE_KEEP:
            out.append(ch)
            continue
        if ch not in _JOINERS and unicodedata.category(ch) in _UNSAFE_CATEGORIES:
            code = ord(ch)
            out.append(("\\x%02x" % code) if code < 0x100 else
                       ("\\u%04x" % code) if code < 0x10000 else ("\\U%08x" % code))
            continue
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Layout helpers (spec 2.10)
# ---------------------------------------------------------------------------

def cols(stream: Any = None, default: int = 80, env: Optional[Mapping[str, str]] = None) -> int:
    """The real terminal width, floored at 20 and never capped."""
    env = os.environ if env is None else env
    width = 0
    stream = sys.stdout if stream is None else stream
    try:
        width = int(os.get_terminal_size(stream.fileno()).columns)
    except (AttributeError, ValueError, OSError, TypeError):
        width = 0
    if width <= 0:
        try:
            width = int(str(env.get("COLUMNS") or "0"))
        except ValueError:
            width = 0
    if width <= 0:
        try:
            width = int(shutil.get_terminal_size((default, 24)).columns)
        except (ValueError, OSError):
            width = default
    return max(20, width)


RULE_MAX = 100


def rule(width: int, role: Optional[str] = "muted", c: Optional[Caps] = None) -> str:
    """A horizontal rule ``min(width - 1, 100)`` cells long.

    The last column stays free everywhere in this module: a line that fills
    it double-wraps on consoles without deferred wrap (legacy conhost).
    """
    c = _c(c)
    glyph = "-" if c.ambiguous_wide else g("rule", c)
    return s(glyph * max(0, min(int(width) - 1, RULE_MAX)), *(r for r in (role,) if r), c=c)


def _hard_break(token: str, width: int):
    """Split one over-long token into chunks of at most ``width`` cells."""
    chunks, current, used = [], [], 0
    for unit, size in _units(token):
        if size and used + size > width and current:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(unit)
        used += size
    if current:
        chunks.append("".join(current))
    return chunks


_FENCE = re.compile(r"^\s*(```|~~~)")


def _verbatim(line: str) -> bool:
    stripped = line.strip()
    return (line.startswith("    ") or line.startswith("\t")
            or (stripped.startswith("|") and stripped.count("|") >= 2))


def _wrap_line(line: str, width: int, first: str, rest: str) -> list:
    words = line.split(" ")
    lines, current, used = [], first, cell_width(first)
    prefix_used = used
    for word in words:
        if word == "":
            continue
        size = cell_width(word)
        gap = 0 if used == prefix_used else 1
        if used + gap + size <= width:
            current += (" " if gap else "") + word
            used += gap + size
            continue
        if used != prefix_used:
            lines.append(current)
            current, used = rest, cell_width(rest)
            prefix_used = used
        room = max(1, width - used)
        if size <= room:
            current += word
            used += size
            continue
        pieces = _hard_break(word, room)
        for piece in pieces[:-1]:
            lines.append(current + piece)
            current, used = rest, cell_width(rest)
            prefix_used = used
            room = max(1, width - used)
        # The last piece may still exceed a narrower continuation room.
        tail = pieces[-1]
        while cell_width(tail) > max(1, width - used):
            head, *more = _hard_break(tail, max(1, width - used))
            lines.append(current + head)
            current, used = rest, cell_width(rest)
            tail = "".join(more)
        current += tail
        used += cell_width(tail)
    lines.append(current)
    return lines


def wrap(text: Any, width: int, indent: str = "", hanging: Optional[str] = None,
         c: Optional[Caps] = None) -> list:
    """Word-wrap ``text`` to ``width`` cells; return a list of lines.

    Wrapping is ANSI-aware and measures cells.  ``indent`` prefixes the first
    line of each paragraph and ``hanging`` (default: ``indent``) every
    continuation.  A word is only split when it alone is wider than the
    line (long URLs, paths).  Fenced code, indented lines and pipe-table
    rows are returned unchanged.
    """
    width = max(1, int(width))
    hanging = indent if hanging is None else hanging
    out = []
    fenced = False
    for raw in str(text).split("\n"):
        line = raw.rstrip()
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced or _verbatim(line):
            out.append(line)
            continue
        if not line:
            out.append("")
            continue
        if cell_width(indent) + cell_width(line) <= width:
            # A line that already fits keeps its own spacing (aligned
            # columns in prose, a leading list indent).
            out.append(indent + line)
            continue
        lead = line[:len(line) - len(line.lstrip(" "))]
        if cell_width(hanging) + len(lead) > width // 2:
            lead = ""
        out.extend(_wrap_line(line.lstrip(" "), width, indent + lead,
                              hanging + lead))
    return out


def table(rows: Sequence[Sequence[Any]], cols: Sequence[Any], width: int,
          gap: int = 2, indent: str = "", fill: bool = False,
          c: Optional[Caps] = None) -> list:
    """Align ``rows`` into columns no wider than ``width - 1`` cells.

    ``cols`` holds one ``(min, max, align)`` spec per column (``align`` is
    ``"<"`` or ``">"``; ``max`` may be ``None`` for "as wide as needed").
    The flexible column is the last one whose ``max`` is ``None`` (or the
    last column when every column is bounded): it takes the width that is
    left -- all of it with ``fill=True`` -- and is elided with the ellipsis
    glyph.  The other columns shrink toward their minimum, right to left,
    when the flexible column could not get its own minimum.

    Cells may carry styling from :func:`s`, so they are *not* sanitised
    here: pass untrusted values (tool arguments, paths, model text) through
    :func:`safe_text` first, or an escape sequence inside them reaches the
    terminal as a zero-width unit.
    """
    c = _c(c)
    if not rows:
        return []
    ncols = len(cols)
    specs = []
    for spec in cols:
        lo, hi, align = (tuple(spec) + (None, None, "<"))[:3]
        specs.append((int(lo or 0), hi, align or "<"))
    flexible = [i for i, spec in enumerate(specs) if spec[1] is None]
    flex = flexible[-1] if flexible else ncols - 1
    natural = [0] * ncols
    for row in rows:
        for i in range(ncols):
            cell = str(row[i]) if i < len(row) and row[i] is not None else ""
            natural[i] = max(natural[i], cell_width(cell))
    widths = []
    for i, (lo, hi, _align) in enumerate(specs):
        w = max(lo, natural[i])
        if hi is not None:
            w = min(w, int(hi))
        widths.append(w)
    available = int(width) - 1 - cell_width(indent) - gap * (ncols - 1)
    flex_min = max(1, specs[flex][0])
    others = [i for i in range(ncols) if i != flex]
    for i in reversed(others):
        excess = sum(widths[j] for j in others) + flex_min - available
        if excess <= 0:
            break
        widths[i] = max(specs[i][0], widths[i] - excess)
    left = available - sum(widths[j] for j in others)
    widths[flex] = max(1, left if fill else min(widths[flex], left))
    lines = []
    for row in rows:
        cells = []
        for i in range(ncols):
            cell = str(row[i]) if i < len(row) and row[i] is not None else ""
            cell = truncate(cell, widths[i], c)
            pad = " " * max(0, widths[i] - cell_width(cell))
            cells.append(pad + cell if specs[i][2] == ">" else cell + pad)
        line = indent + (" " * gap).join(cells)
        lines.append(truncate(line.rstrip(), int(width) - 1, c))
    return lines


def compact_count(value: Any) -> str:
    """``64`` -> ``64``, ``8192`` -> ``8.2k``, ``1250000`` -> ``1.2M``."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n) < 1000:
        return "%d" % n
    if abs(n) < 1_000_000:
        text = "%.1fk" % (n / 1000.0)
    else:
        text = "%.1fM" % (n / 1_000_000.0)
    return text.replace(".0k", "k").replace(".0M", "M")


def duration_label(ms: Any) -> str:
    """``231`` -> ``231ms``, ``75700`` -> ``75.7s``, ``135000`` -> ``2m 15s``.

    Seconds keep one decimal up to 100 s so a typical local turn reads the
    same way the spec's footer shows it (``done 75.7s``).
    """
    try:
        ms = max(0, int(ms))
    except (TypeError, ValueError):
        return str(ms)
    if ms < 1000:
        return "%dms" % ms
    if ms < 100_000:
        return "%.1fs" % (ms / 1000.0)
    minutes, seconds = divmod(ms // 1000, 60)
    return "%dm %02ds" % (minutes, seconds)


def _join(parts: Iterable[str], c: Caps, role: str = "muted") -> str:
    return s(_sep(c), role, c=c).join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Notice component (spec 2.7)
# ---------------------------------------------------------------------------

NOTICE_KINDS = {
    "error": ("fail", "danger", "error"),
    "refused": ("refused", "danger", "refused"),
    "warn": ("warn", "warning", "warn"),
    "skipped": ("skip", "muted", "skipped"),
    "unknown": ("ask", "muted", "unknown"),
    "info": ("note", "muted", "note"),
}
_NOTICE_TITLE_COLUMN = 11


def notice(kind: str, title: Any, detail: Any = None, hint: Any = None,
           width: Optional[int] = None, c: Optional[Caps] = None) -> str:
    """One notice: ``<glyph> <word>  <title>`` plus indented detail and hint.

    Title, detail and hint are untrusted and pass through :func:`safe_text`.
    Returns the lines joined by newlines, with no trailing newline.
    """
    c = _c(c)
    width = cols() if width is None else max(20, int(width))
    glyph, role, word = NOTICE_KINDS.get(kind, NOTICE_KINDS["info"])
    head = s("%s %s" % (g(glyph, c), word), role, *(("strong",) if role == "danger" else ()), c=c)
    # Titles start in one column (11) for every kind, so a run of notices
    # reads as a list: "⊘ refused  /read ..." / "✗ error    ...".
    lead = head + " " * max(2, _NOTICE_TITLE_COLUMN - cell_width(head))
    title_text = " ".join(safe_text(title or "").split())
    room = width - 1
    lines = wrap(title_text, room, indent=lead, hanging="  ", c=c) if title_text else [head]
    if detail:
        for chunk in safe_text(detail).expandtabs(4).split("\n"):
            chunk = chunk.rstrip()
            if not (_verbatim(chunk) or _FENCE.match(chunk)):
                lines.extend(wrap(chunk, room, indent="  ", hanging="    ", c=c))
                continue
            # Preformatted detail (traceback, table row) keeps its own
            # indentation under the notice's two cells and is hard-broken
            # rather than word-wrapped, so the notice still fits.
            body = chunk.lstrip(" ")
            lead = "  " + " " * min(len(chunk) - len(body), max(0, room // 2 - 2))
            lines.extend(lead + piece
                         for piece in _hard_break(body, max(1, room - len(lead))))
    if hint:
        label = s("hint:", "muted", c=c)
        text = " ".join(safe_text(hint).split())
        lines.extend(wrap("%s %s" % (label, text), room, indent="  ",
                          hanging="    ", c=c))
    return "\n".join(line.rstrip() for line in lines)


def turn_header(kind: str = "answer", width: Optional[int] = None,
                c: Optional[Caps] = None) -> str:
    """The line that opens Sonder's turn: ``◈ Sonder`` (accent) or ``✗ error`` (danger).

    A speaker label, as in a chat transcript.  The prompt glyph marks the
    person's turns and this label marks Sonder's, so no rule is drawn
    between them.  Any other ``kind`` is shown as the label itself.
    """
    c = _c(c)
    width = cols() if width is None else max(20, int(width))
    if kind == "error":
        label, role = "%s error" % g("fail", c), "danger"
    else:
        name = "Sonder" if not kind or kind == "answer" else _clean(kind)
        label, role = "%s %s" % (g("mark", c), name), "accent"
    return s(truncate(label, width - 1, c), role, "strong", c=c)


# ---------------------------------------------------------------------------
# Tool calls (spec 2.6)
# ---------------------------------------------------------------------------

def tool_call(title: Any, ok: bool = True, elapsed_ms: Any = None,
              width: Optional[int] = None, c: Optional[Caps] = None) -> list:
    """One tool call: the call, then its result on a dim line underneath.

    ``  ▸ Read File src/a.py`` / ``    └ ✓ ok · 12ms``.  The status glyph
    always carries its word (``✓ ok``, ``✗ failed``), so a failed call reads
    without colour.  Returns two lines that fit ``width - 1`` cells.
    """
    c = _c(c)
    width = cols() if width is None else max(20, int(width))
    limit = width - 1
    name = " ".join(safe_text(title or "").split()) or "tool"
    call = "  %s %s" % (s(g("tool", c), "muted", c=c), truncate(name, max(1, limit - 4), c))
    if ok:
        status = s("%s ok" % g("ok", c), "success", c=c)
    else:
        status = s("%s failed" % g("fail", c), "danger", "strong", c=c)
    parts = [status]
    if elapsed_ms:
        parts.append(s(duration_label(elapsed_ms), "muted", c=c))
    result = "    %s %s" % (s(g("result", c), "muted", c=c), _join(parts, c))
    return [call.rstrip(), truncate(result, limit, c).rstrip()]


# ---------------------------------------------------------------------------
# Markdown (answers)
# ---------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?$")
_MD_BULLET = re.compile(r"^(\s{0,3})([-*+])\s+(.*)$")
_MD_NUMBER = re.compile(r"^(\s{0,3})(\d{1,3}[.)])\s+(.*)$")
_MD_QUOTE = re.compile(r"^>\s?(.*)$")
_MD_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}$")
_MD_INLINE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*)")


def _paint_words(text: str, roles: tuple, c: Caps) -> str:
    """Paint each word on its own, so a wrapped span never carries a style
    across a line break."""
    if not roles:
        return text
    return " ".join(s(word, *roles, c=c) if word else word for word in text.split(" "))


def _md_inline(text: str, c: Caps, base: tuple = ()) -> str:
    """Inline code and bold as styles; their markers are not drawn."""
    out = []
    for part in _MD_INLINE.split(text):
        if len(part) > 2 and part[0] == "`" and part[-1] == "`":
            out.append(_paint_words(part[1:-1], ("info",), c))
        elif len(part) > 4 and part.startswith("**") and part.endswith("**"):
            out.append(_paint_words(part[2:-2], base + ("strong",), c))
        else:
            out.append(_paint_words(part, base, c))
    return "".join(out)


def _code_rule(language: str, width: int, c: Caps) -> str:
    """The rule that opens or closes a code block; the opener names the language."""
    glyph = "-" if c.ambiguous_wide else g("rule", c)
    width = max(8, int(width))
    label = " ".join(safe_text(language).split())
    if not label:
        return s(glyph * width, "muted", c=c)
    label = truncate(label, width - 6, c)
    fill = width - cell_width(label) - 4
    return (s(glyph * 2, "muted", c=c) + " " + s(label, "info", c=c) + " "
            + s(glyph * fill, "muted", c=c))


def markdown(text: Any, width: int, indent: str = "", c: Optional[Caps] = None) -> list:
    """Wrap an answer like :func:`wrap`, drawing its Markdown when colour is on.

    Headings, list markers, quotes, rules, inline code and bold are styled,
    and a fenced block sits between two rules, the first naming its
    language.  The markers that only ask for a style (``#``, ``**``,
    backticks, the fence) are not drawn; code itself is never recoloured or
    rewrapped.  Without colour, or in plain mode, the text comes back
    exactly as :func:`wrap` returns it, so ``NO_COLOR``, dumb terminals and
    screen readers keep the source text.  Pipe tables and indented lines
    stay verbatim either way.
    """
    c = _c(c)
    width = max(1, int(width))
    if c.color == "none" or c.plain:
        return wrap(text, width, indent=indent, c=c)
    bar = max(8, min(width, RULE_MAX) - cell_width(indent))
    out = []
    fenced = False
    for raw in str(text).split("\n"):
        line = raw.rstrip()
        if _FENCE.match(line):
            fenced = not fenced
            language = line.strip()[3:] if fenced else ""
            out.append(indent + _code_rule(language, bar, c))
            continue
        if fenced or _verbatim(line):
            out.append(line)
            continue
        if not line:
            out.append("")
            continue
        if _MD_RULE.match(line):
            out.append(indent + _code_rule("", bar, c))
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            roles = ("accent", "strong") if len(heading.group(1)) <= 2 else ("strong",)
            out.extend(wrap(_md_inline(heading.group(2), c, roles), width,
                            indent=indent, c=c))
            continue
        quote = _MD_QUOTE.match(line)
        if quote:
            # Only the bar is quiet: quoted text is still text to read, and
            # a muted last line would run into the muted footer under it.
            lead = indent + s(g("quote", c), "muted", c=c) + " "
            out.extend(wrap(_md_inline(quote.group(1), c), width,
                            indent=lead, c=c))
            continue
        item = _MD_BULLET.match(line) or _MD_NUMBER.match(line)
        if item:
            pad, marker, body = item.groups()
            shown = g("bullet", c) if marker in ("-", "*", "+") else marker
            lead = indent + pad + s(shown, "accent", c=c) + " "
            hang = indent + pad + " " * (cell_width(shown) + 1)
            out.extend(wrap(_md_inline(body, c), width, indent=lead, hanging=hang, c=c))
            continue
        out.extend(wrap(_md_inline(line, c), width, indent=indent, c=c))
    return [line.rstrip() for line in out]


# ---------------------------------------------------------------------------
# Status line (spec 2.4)
# ---------------------------------------------------------------------------

@dataclass
class StatusState:
    """Persistent session state shown above the prompt."""

    mode: str = "manual"
    tier: str = "code"
    model: str = ""
    ctx_used: Optional[int] = None
    ctx_limit: Optional[int] = None
    agents: int = 0
    lanes: int = 0
    project: str = "default"
    elevated: bool = False
    elevated_reason: str = ""


def _coerce(state: Any, cls):
    if isinstance(state, cls):
        return state
    if isinstance(state, Mapping):
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in state.items() if k in known})
    raise TypeError("expected %s or a mapping" % cls.__name__)


def _status_fields(st: StatusState, c: Caps, *, model: Optional[str], reason: bool,
                   ctx: bool, tier: bool, agents: bool, project: bool) -> list:
    """The status fields, in display order.

    The line is quiet by design (spec 2.4: "its own muted line"): only the
    tier (info) and the mode word (its mode role) carry colour; the model,
    counts and project stay muted so the line never competes with an answer.
    """
    parts = []
    if tier and st.tier:
        parts.append(s(_clean(st.tier), "info", c=c))
    if model:
        parts.append(s(model, "muted", c=c))
    mode_word = s(_clean(st.mode) or "unknown", *mode_roles(st.mode), c=c)
    if st.elevated:
        # Reverse video reads as a badge only with a cell of padding.
        badge = "ELEVATED" if c.color == "none" else " ELEVATED "
        mode_word += " " + s(badge, *mode_roles(st.mode, True), c=c)
        if reason and st.elevated_reason:
            mode_word += " " + s("(%s)" % _clean(st.elevated_reason), "danger", c=c)
    parts.append(mode_word)
    if ctx and st.ctx_limit:
        parts.append(s("ctx %s/%s" % (compact_count(st.ctx_used or 0),
                                      compact_count(st.ctx_limit)), "muted", c=c))
    if agents:
        if st.agents:
            parts.append(s("%d %s" % (st.agents, "agents" if st.agents != 1 else "agent"), "muted", c=c))
        if st.lanes:
            parts.append(s("%d %s" % (st.lanes, "lanes" if st.lanes != 1 else "lane"), "muted", c=c))
    if project and st.project and st.project != "default":
        parts.append(s("proj %s" % _clean(st.project), "muted", c=c))
    return parts


def status_line(state: Any, width: int, c: Optional[Caps] = None) -> str:
    """The one-row status line: ``code · sonder:latest · manual · ctx 64/8.2k``.

    One vocabulary at every width.  Fields leave in priority order
    (project, agents/lanes, elevation reason, model shortened then dropped,
    ctx, tier); the mode word is never dropped.  The result fits in
    ``width - 1`` cells.
    """
    c = _c(c)
    st = _coerce(state, StatusState)
    width = max(1, int(width))
    limit = width - 1
    model = " ".join(safe_text(st.model).split())
    base = dict(model=model or None, reason=True, ctx=True, tier=True,
                agents=width >= 60, project=width >= 80)
    if width < 40:
        base.update(model=None)
    steps = [
        {},
        {"project": False},
        {"project": False, "agents": False},
        {"project": False, "agents": False, "reason": False},
        {"project": False, "agents": False, "reason": False, "model": "shorten"},
        {"project": False, "agents": False, "reason": False, "model": None},
        {"project": False, "agents": False, "reason": False, "model": None, "ctx": False},
        {"project": False, "agents": False, "reason": False, "model": None, "ctx": False,
         "tier": False},
    ]
    line = ""
    for step in steps:
        opts = dict(base)
        opts.update(step)
        if opts["model"] == "shorten":
            if not base["model"]:
                continue
            probe = _join(_status_fields(st, c, **dict(opts, model="\x00")), c)
            room = limit - (cell_width(probe) - 1)
            if room < 6:
                continue
            opts["model"] = truncate(model, room, c)
        line = _join(_status_fields(st, c, **opts), c)
        if cell_width(line) <= limit:
            return line
    return truncate(line, limit, c)


# ---------------------------------------------------------------------------
# Live line (spec 2.6)
# ---------------------------------------------------------------------------

@dataclass
class LiveState:
    """What the in-flight turn is doing right now."""

    phase: str = "routing"
    elapsed_s: float = 0.0
    model: str = ""
    tokens_in: Optional[int] = None
    slow: bool = False
    slow_hint: str = "slow local model? /model fast"


def live_line(state: Any, width: int, c: Optional[Caps] = None) -> str:
    """``◈ working · routing · 12s · sonder:latest · Ctrl-C cancels``.

    Returns one row that fits ``width - 1`` cells (drop order: model,
    token count, cancel hint, slow hint).  In plain mode it is the word form
    ``working (12s)``.  The caller owns redraw (``\\r\\x1b[2K`` prefix) and
    cadence.
    """
    c = _c(c)
    st = _coerce(state, LiveState)
    width = max(1, int(width))
    seconds = max(0, int(st.elapsed_s or 0))
    elapsed = "%ds" % seconds if seconds < 60 else "%dm %02ds" % divmod(seconds, 60)
    if c.plain:
        return truncate("working" if seconds < 15 else "working (%s)" % elapsed, width - 1, c)
    head = s("%s working" % g("mark", c), "accent", "strong", c=c)
    phase = s(" ".join(safe_text(st.phase).split()) or "working", "text", c=c)
    model = " ".join(safe_text(st.model).split())
    tok = ("%s tok in" % compact_count(st.tokens_in)) if st.tokens_in else ""
    cancel = s("Ctrl-C cancels", "muted", c=c)
    slow = s(" ".join(safe_text(st.slow_hint).split()), "warning", c=c) if st.slow else ""
    when = s(elapsed, "muted", c=c)
    # The slow hint is the one actionable item, so it outlives the model
    # name and token count; the cancel hint goes last of all.
    candidates = [
        [head, phase, when, model, tok, slow, cancel],
        [head, phase, when, tok, slow, cancel],
        [head, phase, when, slow, cancel],
        [head, phase, when, slow],
        [head, phase, when, cancel],
        [head, phase, when],
    ]
    line = ""
    for parts in candidates:
        line = _join(parts, c)
        if cell_width(line) <= width - 1:
            return line
    return truncate(line, width - 1, c)


# ---------------------------------------------------------------------------
# Footer (spec 2.6)
# ---------------------------------------------------------------------------

@dataclass
class FooterState:
    """Per-turn metrics; the footer is the only place they appear."""

    elapsed_ms: int = 0
    ok: bool = True
    model_calls: Optional[int] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    tool_calls: Optional[int] = None
    hint: str = ""
    rate: str = "full"  # "full" -> "rate: /pass /fail", "short" -> "/pass /fail", "" -> none


def footer(state: Any, width: int, c: Optional[Caps] = None) -> str:
    """``  done 75.7s · 2 model calls · 2.6k→43 tok · rate: /pass /fail``.

    Muted, indented two cells, no trailing whitespace, fits ``width - 1``.
    """
    c = _c(c)
    st = _coerce(state, FooterState)
    width = max(1, int(width))
    when = duration_label(st.elapsed_ms)
    parts = ["done %s" % when if st.ok else "failed after %s" % when]
    optional = []
    if st.model_calls:
        optional.append("%d model call%s" % (st.model_calls, "" if st.model_calls == 1 else "s"))
    if st.tool_calls:
        optional.append("%d tool%s" % (st.tool_calls, "" if st.tool_calls == 1 else "s"))
    if st.tokens_in is not None and st.tokens_out is not None and (st.tokens_in or st.tokens_out):
        optional.append("%s%s%s tok" % (compact_count(st.tokens_in), g("arrow", c),
                                        compact_count(st.tokens_out)))
    tail = []
    if not st.ok and st.hint:
        tail.append("hint: %s" % " ".join(safe_text(st.hint).split()))
    elif st.ok and st.rate == "full":
        tail.append("rate: /pass /fail")
    elif st.ok and st.rate == "short":
        tail.append("/pass /fail")
    # Drop the metrics before the action; truncate the hint last.
    for count in range(len(optional), -1, -1):
        text = "  " + _sep(c).join(parts + optional[:count] + tail)
        if cell_width(text) <= width - 1:
            break
    else:
        text = truncate("  " + _sep(c).join(parts + tail), width - 1, c)
    if cell_width(text) > width - 1:
        text = truncate(text, width - 1, c)
    return s(text.rstrip(), "muted", c=c)


# ---------------------------------------------------------------------------
# Banner and /about (spec 2.5)
# ---------------------------------------------------------------------------

@dataclass
class BannerState:
    """Everything the startup banner and ``/about`` show.

    The REPL fills it from live runtime reads; nothing here does I/O.
    ``mode_cycle_key`` must be true only when a Shift+Tab handler is
    actually installed for this session.
    """

    persona: str = ""
    model: str = ""
    tier: str = "code"
    mode: str = "manual"
    endpoint: str = "http://127.0.0.1:11435"
    live: bool = False
    # Shown when the caller knows them: the build's version label beside the
    # name, and the working directory on a line of its own.
    version: str = ""
    cwd: str = ""
    mode_cycle_key: bool = False
    behind: int = 0
    restart_required: bool = False
    notices: int = 0
    elevated: bool = False
    elevated_reason: str = ""
    strict: bool = False
    # /about only
    project: str = "default"
    session_id: str = ""
    mode_blurb: str = ""
    update_state: str = ""
    installed_commit: str = ""
    installed_time: str = ""
    running_commit: str = ""
    newest_commit: str = ""
    newest_time: str = ""
    newest_refreshed: bool = False

    @classmethod
    def from_source(cls, source: Optional[Mapping[str, Any]] = None, **fields: Any) -> "BannerState":
        """Build from ``runtime_source_update_status_data`` plus keyword fields."""
        src = dict(source or {})
        try:
            behind = int(src.get("behind") or 0)
        except (TypeError, ValueError):
            behind = 0
        values = dict(
            behind=behind,
            restart_required=bool(src.get("restart_required")),
            update_state=str(src.get("state") or ""),
            installed_commit=str(src.get("installed_commit") or ""),
            installed_time=str(src.get("installed_commit_time") or ""),
            running_commit=str(src.get("running_commit") or ""),
            newest_commit=str(src.get("newest_commit") or ""),
            newest_time=str(src.get("newest_commit_time") or ""),
            newest_refreshed=bool(src.get("remote_ref_refreshed")),
        )
        values.update(fields)
        return cls(**values)


def _clean(value: Any) -> str:
    return " ".join(safe_text(value).split())


def _display_endpoint(endpoint: str, keep_scheme: bool) -> str:
    text = _clean(endpoint)
    if not keep_scheme:
        text = re.sub(r"^[a-z]+://", "", text).rstrip("/")
    return text


def _identity_line(st: BannerState, width: int, c: Caps) -> str:
    limit = width - 1
    name = s("%s sonder" % g("mark", c), "accent", "strong", c=c)
    version = _clean(st.version)
    mode = s(_clean(st.mode) or "unknown", *mode_roles(st.mode), c=c)
    persona = s(_clean(st.persona), "info", c=c) if st.persona else ""
    model_text = _clean(st.model) or "unknown model"
    tier = _clean(st.tier)
    url = _clean(st.endpoint)

    def endpoint(keep_scheme: bool, short: bool) -> str:
        shown = _display_endpoint(url, keep_scheme)
        if st.live:
            return s(link(shown, url, c), "success", c=c)
        if short:
            return s("not listening", "warning", c=c)
        return s("%s not listening" % shown, "warning", c=c)

    def build(model: Optional[str], with_tier: bool, with_persona: bool,
              keep_scheme: bool, short_endpoint: bool, with_endpoint: bool = True,
              with_version: bool = False) -> str:
        parts = [name + (" " + s(version, "muted", c=c) if with_version and version else "")]
        if with_persona and persona:
            parts.append(persona)
        if model is not None:
            label = s(model, "text", c=c)
            if with_tier and tier:
                label += " " + s("(%s)" % tier, "muted", c=c)
            parts.append(label)
        parts.append(mode)
        if with_endpoint:
            parts.append(endpoint(keep_scheme, short_endpoint))
        return _join(parts, c)

    # A narrow terminal gives up the URL scheme first, then the version.
    attempts = [
        dict(model=model_text, with_tier=True, with_persona=True, keep_scheme=True, short_endpoint=False,
             with_version=True),
        dict(model=model_text, with_tier=True, with_persona=True, keep_scheme=False, short_endpoint=False,
             with_version=True),
        dict(model=model_text, with_tier=True, with_persona=True, keep_scheme=False, short_endpoint=False),
        dict(model=model_text, with_tier=True, with_persona=False, keep_scheme=False, short_endpoint=False),
        dict(model=model_text, with_tier=False, with_persona=False, keep_scheme=False, short_endpoint=False),
        dict(model=model_text, with_tier=False, with_persona=False, keep_scheme=False, short_endpoint=True),
    ]
    for opts in attempts:
        line = build(**opts)
        if cell_width(line) <= limit:
            return line
    # Shorten the model to whatever room remains, then drop pieces.  A dead
    # endpoint is the one fact on this line the user must act on, so
    # "not listening" outlives the model name; a live endpoint goes first.
    for with_endpoint in (True, False):
        probe = build(model="", with_tier=False, with_persona=False, keep_scheme=False,
                      short_endpoint=True, with_endpoint=with_endpoint)
        room = limit - cell_width(probe) - cell_width(_sep(c))
        if room >= 6:
            return build(model=truncate(model_text, room, c), with_tier=False,
                         with_persona=False, keep_scheme=False, short_endpoint=True,
                         with_endpoint=with_endpoint)
        if with_endpoint and not st.live:
            line = build(model=None, with_tier=False, with_persona=False,
                         keep_scheme=False, short_endpoint=True)
            if cell_width(line) <= limit:
                return line
    line = build(model=None, with_tier=False, with_persona=False, keep_scheme=False,
                 short_endpoint=True, with_endpoint=False)
    return truncate(line, limit, c)


def _cwd_line(st: BannerState, width: int, c: Caps) -> str:
    """``  cwd ~/code/engine``: the folder file commands start from.

    A path too long for the row keeps its end, the part that names where
    you are.
    """
    label = "  cwd"
    path = _clean(st.cwd)
    room = max(4, width - 1 - cell_width(label) - 1)
    if cell_width(path) > room:
        dots = g("ellipsis", c)
        keep = max(1, room - cell_width(dots))
        while path and cell_width(path) > keep:
            path = path[1:]
        path = dots + path
    return s(label, "muted", c=c) + " " + s(path, "text", c=c)


def _hint_line(st: BannerState, width: int, c: Caps) -> str:
    items = ["/help commands",
             "Shift+Tab mode" if st.mode_cycle_key else "/mode to switch",
             "/about details",
             "Ctrl-D quits"]
    for count in range(len(items), 0, -1):
        text = "  " + _sep(c).join(items[:count])
        if cell_width(text) <= width - 1:
            return s(text, "muted", c=c)
    return s(truncate("  /help", width - 1, c), "muted", c=c)


def _flag_line(glyph_role: str, text: str, text_role: str, width: int, c: Caps,
               glyph: str = "warn") -> list:
    lead = "  " + s(g(glyph, c), glyph_role, "strong", c=c) + " "
    body = wrap(text, max(8, width - 1 - 4), c=c)
    lines = [lead + s(body[0], text_role, c=c)]
    lines.extend("    " + s(rest, text_role, c=c) for rest in body[1:])
    return lines


def banner(state: Any, width: int, c: Optional[Caps] = None) -> str:
    """The startup banner (spec 2.5), one design on every platform.

    Line 1 is identity: mark, name and version, persona, model (tier), mode,
    endpoint coloured by liveness.  The working directory follows on its
    own line when the caller gives one, then a muted hint.  Optional
    warning lines follow only when they call for action (behind > 0,
    restart required, queued startup notices, elevation, strict).  There is
    no rule.  Returns the lines joined by newlines, no trailing newline;
    every line fits ``width - 1`` cells.
    """
    c = _c(c)
    st = _coerce(state, BannerState)
    width = max(20, int(width))
    lines = [_identity_line(st, width, c)]
    if _clean(st.cwd):
        lines.append(_cwd_line(st, width, c))
    lines.append(_hint_line(st, width, c))
    if st.elevated:
        reason = _clean(st.elevated_reason)
        text = "ELEVATED" + (" %s %s" % (g("sep", c), reason) if reason else "")
        lines.extend(_flag_line("danger", text, "danger", width, c))
    if st.behind and st.behind > 0:
        n = int(st.behind)
        lines.extend(_flag_line(
            "warning", "%d commit%s behind%s/update" % (n, "" if n == 1 else "s", _sep(c)),
            "warning", width, c))
    if st.restart_required:
        lines.extend(_flag_line("warning", "restart required%s/restart" % _sep(c),
                                "warning", width, c))
    if st.strict:
        lines.extend(_flag_line("warning", "strict%spinned to the sonder alias" % _sep(c),
                                "warning", width, c))
    if st.notices and st.notices > 0:
        n = int(st.notices)
        lines.extend(_flag_line("warning", "%d startup notice%s%s/logs" % (
            n, "" if n == 1 else "s", _sep(c)), "muted", width, c))
    return "\n".join(line.rstrip() for line in lines)


def _terminal_words(c: Caps) -> str:
    """What was detected about the terminal, and which palette that gives.

    A terminal whose background is not known is drawn with its own 16
    colours; the row says so and names the switch, because nothing else on
    screen explains why the accent is the terminal's blue.
    """
    sep = _sep(c)
    if c.color == "none":
        words = "no colour"
    else:
        words = {"16": "16 colours", "256": "256 colours",
                 "truecolor": "24-bit colour"}[c.color]
        if c.palette != "16":
            words += "%s%s theme%sapp palette" % (sep, c.theme, sep)
        elif c.color != "16":
            words += ("%stheme unknown, so the terminal's own 16 colours"
                      "%sSONDER_THEME=dark or light uses the app palette" % (sep, sep))
    if c.glyphs == "ascii":
        words += sep + "ASCII glyphs"
    return words


def about_lines(state: Any, width: Optional[int] = None, c: Optional[Caps] = None) -> list:
    """Full provenance for ``/about``: identity, endpoint, mode, source, update."""
    c = _c(c)
    st = _coerce(state, BannerState)
    width = cols() if width is None else max(20, int(width))

    def short(commit: str) -> str:
        return _clean(commit)[:12] or "unknown"

    def at(commit: str, when: str) -> str:
        return short(commit) + (" @ %s" % _clean(when) if when else "")

    rows = []
    if _clean(st.version):
        rows.append(("version", _clean(st.version)))
    if _clean(st.cwd):
        rows.append(("cwd", _clean(st.cwd)))
    rows += [
        ("persona", _clean(st.persona) or "(none)"),
        ("model", "%s (%s)" % (_clean(st.model) or "unknown", _clean(st.tier) or "?")),
        ("project", _clean(st.project) or "(none)"),
    ]
    if st.session_id:
        rows.append(("session", _clean(st.session_id)))
    rows.append(("endpoint", "%s %s" % (_clean(st.endpoint),
                                         "listening" if st.live else "not listening")))
    mode = _clean(st.mode) or "unknown"
    if st.elevated:
        mode += " ELEVATED" + (" (%s)" % _clean(st.elevated_reason) if st.elevated_reason else "")
    rows.append(("mode", mode))
    if st.mode_blurb:
        rows.append(("", _clean(st.mode_blurb)))
    rows.append(("installed", at(st.installed_commit, st.installed_time)))
    running = short(st.running_commit) if st.running_commit else "unavailable"
    if st.restart_required:
        running += " (restart required)"
    rows.append(("running", running))
    rows.append(("newest" if st.newest_refreshed else "newest known",
                 at(st.newest_commit, st.newest_time)))
    state_word = _clean(st.update_state) or "unknown"
    rows.append(("update", "%s%sbehind %d%s/updatecheck%s/update" % (
        state_word, _sep(c), max(0, int(st.behind or 0)), _sep(c), _sep(c))))
    rows.append(("terminal", _terminal_words(c)))
    label_w = max(cell_width(r[0]) for r in rows)
    out = [s("%s sonder" % g("mark", c), "accent", "strong", c=c)]
    for label, value in rows:
        prefix = "  " + label.ljust(label_w) + "  "
        body = wrap(value, width - 1, indent=prefix, hanging=" " * cell_width(prefix), c=c)
        first = body[0]
        if label:
            first = "  " + s(label.ljust(label_w), "muted", c=c) + first[2 + label_w:]
        out.append(first)
        out.extend(body[1:])
    return [line.rstrip() for line in out]
