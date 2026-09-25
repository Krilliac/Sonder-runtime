"""Contract tests for the REPL style foundation (REPL redesign spec 2.1-2.10).

The matrix renders every chrome component under each colour level, theme,
glyph set and width the spec names, and checks the invariants that make the
chrome safe to print: nothing wider than the terminal (by cell width), pure
ASCII under ASCII glyphs, no ESC byte without colour, readable contrast, and
one status-line vocabulary at every width.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path
import random
import re
import unicodedata

import pytest

from sonder_runtime.interfaces.repl import style as S

pytestmark = pytest.mark.unit

COLORS = ("none", "16", "256", "truecolor")
THEMES = ("dark", "light", "unknown")
GLYPHS = ("unicode", "ascii")
WIDTHS = (30, 40, 50, 60, 80, 110)
BACKGROUNDS = {"dark": ((0x1E, 0x1E, 0x1E), (0, 0, 0)), "light": ((255, 255, 255),)}


class _Stream:
    def __init__(self, tty=True, encoding="utf-8"):
        self._tty = tty
        self.encoding = encoding

    def isatty(self):
        return self._tty


def _caps(color, theme, glyphs, plain=False):
    return S.Caps(color=color, theme=theme, glyphs=glyphs, plain=plain,
                  motion=color != "none" and not plain)


BANNERS = {
    "live": S.BannerState(persona="coder", model="sonder:latest", tier="code",
                          mode="manual", endpoint="http://127.0.0.1:11435", live=True),
    "down": S.BannerState(persona="coder", model="qwen2.5-coder:7b-instruct-q4_K_M",
                          tier="code", mode="acceptEdits", live=False, notices=3),
    "loud": S.BannerState(persona="researcher", model="sonder:latest", tier="general",
                          mode="auto", live=False, behind=12, restart_required=True,
                          elevated=True, elevated_reason="admin session for selfmod deploy",
                          strict=True, notices=1, mode_cycle_key=True),
}
STATUS = {
    "plain": S.StatusState(mode="manual", tier="code", model="sonder:latest",
                           ctx_used=64, ctx_limit=8192),
    "busy": S.StatusState(mode="acceptEdits", tier="code",
                          model="qwen2.5-coder:7b-instruct-q4_K_M", ctx_used=5100,
                          ctx_limit=32768, agents=2, lanes=1, project="alpha-site"),
    "elevated": S.StatusState(mode="manual", tier="code", model="sonder:latest",
                              ctx_used=64, ctx_limit=8192, agents=2, project="foo",
                              elevated=True, elevated_reason="dev bypass"),
}


def _render_all(c, width):
    """Every component at ``width``; yields (name, text)."""
    for key, st in BANNERS.items():
        yield "banner:%s" % key, S.banner(st, width, c)
        yield "about:%s" % key, "\n".join(S.about_lines(st, width, c))
    for key, st in STATUS.items():
        yield "status:%s" % key, S.status_line(st, width, c)
    yield "live", S.live_line(S.LiveState(phase="model call 1/2", elapsed_s=42,
                                          model="sonder:latest", tokens_in=2600,
                                          slow=True), width, c)
    yield "footer:ok", S.footer(S.FooterState(elapsed_ms=75700, model_calls=2,
                                              tokens_in=2600, tokens_out=43), width, c)
    yield "footer:fail", S.footer(S.FooterState(elapsed_ms=231, ok=False,
                                                hint="the model endpoint refused the connection; start ollama"),
                                  width, c)
    yield "notice", S.notice("refused", "/read /etc/shadow", "path is outside allowed roots",
                             "add a project folder to SONDER_FILE_ROOTS", width=width, c=c)
    yield "notice:long", S.notice("error", "x" * 90, "detail " * 30, None, width=width, c=c)
    yield "header", S.turn_header("answer", width, c)
    yield "header:error", S.turn_header("error", width, c)
    yield "rule", S.rule(width, c=c)
    yield "table", "\n".join(S.table(
        [("/model [tier|name]", "switch model or tier", ""),
         ("/run <cmd>", "run a command in the project sandbox with a timeout", "[runs]")],
        [(6, 20, "<"), (8, None, "<"), (0, 8, ">")], width, indent="  ", fill=True, c=c))
    yield "wrap", "\n".join(S.wrap(
        "RAII stands for Resource Acquisition Is Initialization, a technique where "
        "resources are acquired when created and released when destroyed. See "
        "https://en.cppreference.com/w/cpp/language/raii_resource_acquisition_is_initialization",
        width - 1, indent="  ", hanging="    ", c=c))


MATRIX = list(itertools.product(COLORS, THEMES, GLYPHS, WIDTHS))


@pytest.mark.parametrize("color,theme,glyphs,width", MATRIX)
def test_matrix_invariants(color, theme, glyphs, width):
    c = _caps(color, theme, glyphs)
    for name, text in _render_all(c, width):
        for line in text.split("\n"):
            assert S.cell_width(line) <= width - 1, (name, width, line)
            assert line == line.rstrip(" "), (name, "trailing space", line)
        if glyphs == "ascii":
            plain = S.strip_ansi(text)
            assert all(ord(ch) < 128 for ch in plain), (name, plain)
        if color == "none":
            assert "\x1b" not in text, (name, text)


@pytest.mark.parametrize("width", WIDTHS)
def test_plain_live_line_is_words_only(width):
    c = _caps("none", "unknown", "ascii", plain=True)
    line = S.live_line(S.LiveState(phase="routing", elapsed_s=30), width, c)
    assert line == "working (30s)"
    assert "\r" not in line


# ---------------------------------------------------------------------------
# Contrast (WCAG 2.x relative luminance)
# ---------------------------------------------------------------------------

def _linear(channel):
    value = channel / 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(rgb):
    r, g, b = (_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _xterm256(index):
    if index >= 232:
        v = 8 + 10 * (index - 232)
        return (v, v, v)
    index -= 16
    levels = (0, 95, 135, 175, 215, 255)
    return (levels[index // 36], levels[(index // 6) % 6], levels[index % 6])


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("role", sorted(S._TRUE))
def test_token_contrast_at_least_4_5(theme, role):
    index = 0 if theme == "dark" else 1
    for background in BACKGROUNDS[theme]:
        true_rgb = S._TRUE[role][index]
        cell_rgb = _xterm256(S._C256[role][index])
        assert _contrast(true_rgb, background) >= 4.5, (role, theme, "truecolor")
        assert _contrast(cell_rgb, background) >= 4.5, (role, theme, "256")


def test_palette_escapes_follow_theme_and_level():
    assert S.s("x", "accent", c=_caps("truecolor", "dark", "unicode")).startswith("\x1b[38;2;99;214;200m")
    assert S.s("x", "accent", c=_caps("256", "light", "unicode")).startswith("\x1b[38;5;23m")
    # Unknown theme falls back to the ANSI-16 roles the terminal theme adapts.
    assert S.s("x", "accent", c=_caps("truecolor", "unknown", "unicode")).startswith("\x1b[36m")
    assert S.s("x", "accent", c=_caps("none", "dark", "unicode")) == "x"


def test_nested_styles_restore_outer_role():
    c = _caps("16", "unknown", "unicode")
    inner = S.s("mode", "warning", c=c)
    outer = S.s("a %s b" % inner, "muted", c=c)
    assert outer == "\x1b[2ma \x1b[33mmode\x1b[0m\x1b[2m b\x1b[0m"


# ---------------------------------------------------------------------------
# safe_text
# ---------------------------------------------------------------------------

_BIDI = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c")


def _assert_inert(text):
    for ch in text:
        code = ord(ch)
        if ch in "\n\t":
            continue
        assert code >= 0x20 and not (0x7F <= code <= 0x9F), repr(ch)
        assert ch not in _BIDI, repr(ch)
        assert unicodedata.category(ch) not in ("Cc", "Cs", "Zl", "Zp"), repr(ch)


def test_safe_text_escapes_terminal_controls():
    hostile = "ok\x1b]52;c;ZXZpbA==\x07\x1b[2J\x9b31m\u202eevil\u2066\r\nnext\tcol\x00"
    out = S.safe_text(hostile)
    _assert_inert(out)
    assert "\\x1b]52;c;ZXZpbA==\\x07" in out
    assert "\\u202e" in out and "\\x9b" in out and "\\x0d\n" in out
    assert out.endswith("\tcol\\x00")


def test_safe_text_keeps_ordinary_unicode():
    text = "caf\u00e9 \u4e2d\u6587 \U0001f468\u200d\U0001f4bb e\u0301 \u0645\u0631\u062d\u0628\u0627"
    assert S.safe_text(text) == text


def test_safe_text_fuzz():
    rng = random.Random(0x50DE5)
    pool = [chr(i) for i in range(0, 0x250)] + sorted(_BIDI) + [
        "\u2028", "\u2029", "\ufeff", "\u200b", "\ud800", "\U000e0001"]
    for _ in range(400):
        sample = "".join(rng.choice(pool) for _ in range(rng.randint(0, 120)))
        _assert_inert(S.safe_text(sample))
    for _ in range(200):
        sample = "".join(chr(rng.randint(0, 0x10FFFF)) for _ in range(40))
        _assert_inert(S.safe_text(sample))


def test_notice_sanitizes_every_field():
    c = _caps("16", "unknown", "unicode")
    text = S.notice("error", "bad\x1b[31m", "detail\x07", "hint\u202e", width=80, c=c)
    visible = S.strip_ansi(text)
    assert "\\x1b[31m" in visible and "\\x07" in visible and "\\u202e" in visible
    _assert_inert(visible)


# ---------------------------------------------------------------------------
# Status line
# ---------------------------------------------------------------------------

def _fields(line, c):
    return [f for f in S.strip_ansi(line).split(" %s " % S.g("sep", c))]


@pytest.mark.parametrize("glyphs", GLYPHS)
@pytest.mark.parametrize("key", sorted(STATUS))
def test_status_vocabulary_identical_across_widths(key, glyphs):
    c = _caps("none", "unknown", glyphs)
    state = STATUS[key]
    full = _fields(S.status_line(state, 400, c), c)
    ellipsis = S.g("ellipsis", c)
    for width in (20,) + WIDTHS + (200,):
        line = S.status_line(state, width, c)
        assert S.cell_width(line) <= width - 1
        fields = _fields(line, c)
        for fld in fields:
            assert len(fld) > 1, (width, line)  # never a bare single letter
            if fld in full:
                continue
            # The only permitted variants: model shortened, elevation reason dropped.
            if fld.endswith(ellipsis):
                assert state.model.startswith(fld[: -len(ellipsis)]), (width, fld)
            else:
                assert any(f.startswith(fld) for f in full), (width, fld, full)
        assert any(f.startswith(state.mode) for f in fields), (width, line)


def test_status_line_width_variants():
    c = _caps("none", "unknown", "unicode")
    st = STATUS["elevated"]
    assert S.status_line(st, 110, c) == (
        "code · sonder:latest · manual ELEVATED (dev bypass) · ctx 64/8.2k · 2 agents · proj foo")
    assert "proj" not in S.status_line(st, 70, c)
    at50 = S.status_line(st, 50, c)
    assert "agents" not in at50 and "…" in at50
    assert S.status_line(st, 30, c) == "code · manual ELEVATED"
    assert S.status_line(STATUS["plain"], 30, c) == "code · manual · ctx 64/8.2k"


def test_status_line_same_before_and_after_a_turn():
    c = _caps("none", "unknown", "unicode")
    before = S.status_line(S.StatusState(model="sonder:latest", ctx_used=0, ctx_limit=8192), 110, c)
    after = S.status_line(S.StatusState(model="sonder:latest", ctx_used=64, ctx_limit=8192), 110, c)
    assert _fields(before, c)[:3] == _fields(after, c)[:3]
    assert before.replace("ctx 0/", "ctx 64/") == after


# ---------------------------------------------------------------------------
# Banner and /about
# ---------------------------------------------------------------------------

def test_banner_default_is_two_lines_and_has_no_rule():
    c = _caps("none", "unknown", "unicode")
    text = S.banner(BANNERS["live"], 80, c)
    assert text.split("\n") == [
        "◈ sonder · coder · sonder:latest (code) · manual · http://127.0.0.1:11435",
        "  /help commands · /mode to switch · /about details · Ctrl-D quits",
    ]
    assert "─" not in text and "installed" not in text


def test_banner_mockup_at_80_with_notices():
    c = _caps("none", "unknown", "unicode")
    state = S.BannerState(persona="coder", model="sonder:latest", tier="code",
                          live=True, notices=3)
    lines = S.banner(state, 80, c).split("\n")
    assert len(lines) <= 3
    assert lines[2] == "  ! 3 startup notices · /logs"


@pytest.mark.parametrize("width", WIDTHS)
def test_shift_tab_hint_only_when_key_works(width):
    c = _caps("none", "unknown", "unicode")
    assert "Shift+Tab" not in S.banner(BANNERS["live"], width, c)
    state = S.BannerState(model="sonder:latest", live=True, mode_cycle_key=True)
    if width >= 40:
        assert "Shift+Tab mode" in S.banner(state, width, c)


def test_update_lines_only_when_actionable():
    c = _caps("none", "unknown", "unicode")
    quiet = S.BannerState.from_source({"state": "ahead", "behind": 0}, model="m", live=True)
    assert "behind" not in S.banner(quiet, 110, c)
    assert "/update" not in S.banner(quiet, 110, c)
    loud = S.banner(BANNERS["loud"], 110, c)
    assert "! 12 commits behind · /update" in loud
    assert "! restart required · /restart" in loud
    assert "! ELEVATED · admin session for selfmod deploy" in loud
    assert "! strict · pinned to the sonder alias" in loud


def test_banner_endpoint_coloured_by_liveness():
    c = _caps("16", "unknown", "unicode")
    live = S.banner(BANNERS["live"], 110, c).split("\n")[0]
    down = S.banner(BANNERS["down"], 110, c).split("\n")[0]
    assert "\x1b[32mhttp://127.0.0.1:11435" in live
    assert "\x1b[33m127.0.0.1:11435 not listening" in down


def test_banner_is_one_design_across_platform_caps():
    """The same state renders the same words whatever the terminal supports."""
    words = {S.strip_ansi(S.banner(BANNERS["live"], 80, _caps(col, th, "unicode")))
             for col in COLORS for th in THEMES}
    assert len(words) == 1


def test_banner_ascii_glyphs():
    c = _caps("none", "unknown", "ascii")
    first = S.banner(BANNERS["live"], 80, c).split("\n")[0]
    assert first == "# sonder | coder | sonder:latest (code) | manual | http://127.0.0.1:11435"


def test_about_lines_carry_provenance():
    state = S.BannerState.from_source(
        {"installed_commit": "49b6dee3a502abcdef", "installed_commit_time": "2026-09-25T09:26:31+00:00",
         "running_commit": "49b6dee3a502", "newest_commit": "6b31a39c4a53ffff",
         "newest_commit_time": "2026-09-24T21:31:40+00:00", "state": "ahead", "behind": 0},
        persona="coder", model="sonder:latest", project="default", session_id="s-1",
        mode_blurb="ask before anything that is not a read")
    text = "\n".join(S.about_lines(state, 80, _caps("none", "unknown", "unicode")))
    for needle in ("installed     49b6dee3a502 @ 2026-09-25T09:26:31+00:00",
                   "running       49b6dee3a502", "newest known  6b31a39c4a53",
                   "update        ahead · behind 0 · /updatecheck · /update",
                   "session       s-1", "ask before anything"):
        assert needle in text


# ---------------------------------------------------------------------------
# Notices, footer, header
# ---------------------------------------------------------------------------

def test_notice_mockup():
    c = _caps("none", "unknown", "unicode")
    assert S.notice("refused", "/read /etc/shadow", "path is outside allowed roots",
                    "add a project folder to SONDER_FILE_ROOTS", width=80, c=c) == (
        "⊘ refused  /read /etc/shadow\n"
        "  path is outside allowed roots\n"
        "  hint: add a project folder to SONDER_FILE_ROOTS")


@pytest.mark.parametrize("kind,word", [("error", "x error"), ("refused", "x refused"),
                                       ("warn", "! warn"), ("skipped", "- skipped"),
                                       ("unknown", "? unknown"), ("info", "* note")])
def test_notice_kinds_have_words(kind, word):
    c = _caps("none", "unknown", "ascii")
    assert S.notice(kind, "t", width=40, c=c).startswith(word)


def test_footer_forms():
    c = _caps("none", "unknown", "unicode")
    assert S.footer(S.FooterState(elapsed_ms=75700, model_calls=2, tokens_in=2600,
                                  tokens_out=43), 80, c) == (
        "  done 75.7s · 2 model calls · 2.6k→43 tok · rate: /pass /fail")
    assert S.footer(S.FooterState(elapsed_ms=231, ok=False, hint="start ollama"), 80, c) == (
        "  failed after 231ms · hint: start ollama")
    assert S.footer(S.FooterState(elapsed_ms=1200, rate="short"), 80, c) == "  done 1.2s · /pass /fail"


def test_turn_header_never_doubles_label():
    c = _caps("none", "unknown", "unicode")
    assert S.turn_header("error", 40, c).startswith("✗ error ─")
    assert S.turn_header("answer", 200, c).count("─") == 100 - len("◈ answer ")


# ---------------------------------------------------------------------------
# wrap / table / cols
# ---------------------------------------------------------------------------

def test_wrap_never_splits_words_that_fit():
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    for width in range(12, 60):
        lines = S.wrap(text, width)
        assert " ".join(" ".join(lines).split()) == text
        assert all(S.cell_width(line) <= width for line in lines)


def test_wrap_hanging_indent_and_long_tokens():
    lines = S.wrap("see https://example.com/" + "a" * 60 + " now", 30, indent="- ", hanging="  ")
    assert lines[0].startswith("- ") and all(line.startswith("  ") for line in lines[1:])
    assert all(S.cell_width(line) <= 30 for line in lines)
    assert "".join(line[2:] for line in lines).replace(" ", "").startswith("seehttps://example.com/")


def test_wrap_leaves_code_tables_and_indented_lines():
    long_code = "x = " + "1 + " * 30 + "1"
    text = "```\n%s\n```\n    indented %s\n| a | b |\nshort prose" % (long_code, "y" * 80)
    lines = S.wrap(text, 30)
    assert long_code in lines and "| a | b |" in lines
    assert any(line.startswith("    indented") and len(line) > 30 for line in lines)


def test_wrap_keeps_spacing_of_lines_that_fit_and_list_indent():
    text = "  domain/      rules\n  - a list item that is long enough to need wrapping here"
    lines = S.wrap(text, 30)
    assert lines[0] == "  domain/      rules"
    assert lines[1].startswith("  - a list") and all(l.startswith("  ") for l in lines[1:])
    assert all(S.cell_width(l) <= 30 for l in lines)


def test_wrap_is_ansi_and_cell_aware():
    c = _caps("16", "unknown", "unicode")
    text = " ".join(S.s(w, "accent", c=c) for w in ["\u4e2d\u6587\u5b57"] * 10)
    for line in S.wrap(text, 14, c=c):
        assert S.cell_width(line) <= 14


def test_table_elides_flexible_column():
    c = _caps("none", "unknown", "unicode")
    lines = S.table([("a", "b" * 50, "[runs]")], [(1, 5, "<"), (1, None, "<"), (0, 8, ">")], 30, c=c)
    assert len(lines) == 1 and S.cell_width(lines[0]) == 29
    assert lines[0].startswith("a  bbb") and lines[0].endswith("b…  [runs]")


def test_cols_floor_and_no_cap():
    class NoTTY:
        def fileno(self):
            raise OSError

    assert S.cols(NoTTY(), env={"COLUMNS": "7"}) == 20
    assert S.cols(NoTTY(), env={"COLUMNS": "300"}) == 300


# ---------------------------------------------------------------------------
# Capability detection (spec 2.1)
# ---------------------------------------------------------------------------

def _detect(env, tty=True, encoding="utf-8", platform="posix", vt=None):
    return S.caps(env=env, stream=_Stream(tty, encoding), platform=platform, vt_enable=vt)


@pytest.mark.parametrize("env,tty,expected", [
    ({"TERM": "xterm-256color", "NO_COLOR": "1", "FORCE_COLOR": "1"}, True, "none"),
    ({"TERM": "dumb", "FORCE_COLOR": "1"}, False, "16"),
    ({"TERM": "xterm-256color", "CLICOLOR_FORCE": "1"}, False, "256"),
    ({"TERM": "dumb"}, True, "none"),
    ({"TERM": ""}, True, "none"),
    ({"TERM": "xterm-256color"}, False, "none"),
    ({"TERM": "xterm", "COLORTERM": "truecolor"}, True, "truecolor"),
    ({"TERM": "xterm-256color"}, True, "256"),
    ({"TERM": "xterm"}, True, "16"),
])
def test_color_detection_order(env, tty, expected):
    assert _detect(env, tty).color == expected


def test_windows_vt_failure_means_no_colour_and_ascii():
    got = _detect({"WT_SESSION": "1"}, platform="nt", vt=lambda: False)
    assert (got.color, got.glyphs, got.links) == ("none", "ascii", False)
    ok = _detect({"WT_SESSION": "1", "COLORTERM": "truecolor"}, platform="nt", vt=lambda: True)
    assert (ok.color, ok.glyphs, ok.links) == ("truecolor", "unicode", True)
    legacy = _detect({}, platform="nt", vt=lambda: True)
    assert legacy.glyphs == "ascii" and legacy.color == "16"
    raising = _detect({"WT_SESSION": "1"}, platform="nt", vt=lambda: 1 / 0)
    assert raising.color == "none"


def test_theme_detection():
    assert _detect({"TERM": "xterm", "SONDER_THEME": "light"}).theme == "light"
    assert _detect({"TERM": "xterm", "COLORFGBG": "15;0"}).theme == "dark"
    assert _detect({"TERM": "xterm", "COLORFGBG": "0;15"}).theme == "light"
    assert _detect({"TERM": "xterm", "COLORFGBG": "0;default;7"}).theme == "light"
    assert _detect({"TERM": "xterm"}).theme == "unknown"
    assert _detect({"TERM": "xterm", "COLORTERM": "truecolor"}).palette == "16"


def test_glyph_and_plain_switches():
    assert _detect({"TERM": "xterm"}, encoding="ascii").glyphs == "ascii"
    assert _detect({"TERM": "xterm"}, encoding="cp1252").glyphs == "ascii"
    assert _detect({"TERM": "xterm", "SONDER_GLYPHS": "ascii"}).glyphs == "ascii"
    assert _detect({"TERM": "xterm", "SONDER_GLYPHS": "unicode"}, encoding="ascii").glyphs == "unicode"
    assert _detect({"TERM": "xterm", "LANG": "ja_JP.UTF-8"}).glyphs == "ascii"
    plain = _detect({"TERM": "xterm-256color", "SONDER_PLAIN": "1"})
    assert (plain.plain, plain.motion, plain.glyphs, plain.color) == (True, False, "ascii", "256")
    dumb = _detect({"TERM": "dumb"})
    assert (dumb.plain, dumb.motion, dumb.glyphs, dumb.color) == (True, False, "ascii", "none")
    no_color = _detect({"TERM": "xterm", "NO_COLOR": "1"})
    assert no_color.glyphs == "unicode" and not no_color.motion


def test_links_allow_list():
    assert _detect({"TERM": "xterm", "VTE_VERSION": "7000"}).links
    assert _detect({"TERM": "xterm-kitty"}).links
    assert _detect({"TERM": "xterm", "TERM_PROGRAM": "vscode"}).links
    assert not _detect({"TERM": "xterm"}).links
    assert not _detect({"TERM": "xterm", "VTE_VERSION": "7000", "NO_COLOR": "1"}).links


def test_caps_cache_and_injection_do_not_mix():
    S.reset_caps()
    try:
        pinned = S.set_caps(_caps("none", "unknown", "ascii"))
        assert S.caps() is pinned
        _detect({"TERM": "xterm-256color"})
        assert S.caps() is pinned
        assert S.g("mark") == "#"
    finally:
        S.reset_caps()


def test_link_is_inert_without_caps_and_rejects_control_urls():
    on = S.Caps(color="16", links=True)
    assert S.link("x", "http://a", on) == "\x1b]8;;http://a\x1b\\x\x1b]8;;\x1b\\"
    assert S.link("x", "http://a\x07", on) == "x"
    assert S.link("x", "http://a", S.Caps()) == "x"


def test_style_module_imports_only_stdlib():
    source = Path(S.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("sonder_runtime")
        elif isinstance(node, ast.Import):
            assert not any(a.name.startswith("sonder_runtime") for a in node.names)
    assert not re.search(r"\bthreading\b|\bprint\(", source)
