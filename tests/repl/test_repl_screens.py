"""Golden screens and terminal contracts for the interactive REPL (lane C).

Each variant drives one real ``sonder repl`` in a pty (``pty_harness``)
against a fake Ollama (``fake_ollama``) through the same scenario: startup,
``/help``, ``/model``, ``/status``, an unknown command, a refusal, a turn in
progress (seconds masked), the finished turn, a sanitized injection answer,
the error panel, and a declined approval.  Every step's screen is compared
with ``goldens/<step>__<variant>.txt``; ``SONDER_UPDATE_GOLDENS=1`` rewrites
them.  The byte-level checks read the raw pty log: no ESC at all under
``NO_COLOR`` / ``TERM=dumb`` / ``SONDER_PLAIN``, no JSON on the live line, no
injected control byte anywhere.
"""

from __future__ import annotations

import re
import tempfile
import time

import pytest

pexpect = pytest.importorskip("pexpect")
pytest.importorskip("pyte")

from tests.repl.fake_ollama import FakeOllama  # noqa: E402
from tests.repl.pty_harness import (  # noqa: E402
    ReplSession, check_golden, normalize, visible_width,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(pexpect, "spawn"), reason="needs a POSIX pty"),
]

VARIANTS = {
    "c80": (80, {}),
    "c60": (60, {}),
    "c50": (50, {}),
    "nocolor80": (80, {"NO_COLOR": "1"}),
    "dumb80": (80, {"TERM": "dumb"}),
    "plain80": (80, {"SONDER_PLAIN": "1"}),
}
ESCAPE_FREE = ("nocolor80", "dumb80")
NO_REDRAW = ("nocolor80", "dumb80", "plain80")


@pytest.fixture(scope="module")
def fake():
    server = FakeOllama()
    yield server
    server.close()


def _run_scenario(fake, cols, env):
    home = tempfile.mkdtemp(prefix="sonder-repl-screens-")
    fake._release.clear()
    fake.holding.clear()
    session = ReplSession(home, fake.url, cols=cols, env=env)
    screens = {}
    try:
        session.wait_prompt()
        screens["startup"] = session.screen()

        def step(name, line):
            mark = session.mark()
            session.send_line(line)
            session.wait_prompt(mark)
            screens[name] = session.screen(mark)
            return mark

        step("help", "/help")
        step("model", "/model")
        step("status", "/status")
        step("unknown", "/hlep")
        step("refusal", "/read /etc/shadow")

        mark = session.mark()
        session.send_line("hold on, what is RAII?")
        assert fake.holding.wait(60), "the turn never reached the model"
        time.sleep(1.5)
        session.settle(0.3)
        screens["turn_progress"] = session.screen(mark)
        fake.release()
        session.wait_prompt(mark)
        screens["turn_done"] = session.screen(mark)

        injection_mark = step("injection", "please inject something")
        injection_bytes = session.log[injection_mark:]
        step("error_panel", "explode now")

        mark = session.mark()
        session.send_line("/runtime status")
        session.wait_for(b"run it?", mark)
        session.settle(0.3)
        session.send_line("/env")
        session.wait_prompt(mark)
        screens["approval_declined"] = session.screen(mark)
    finally:
        session.close()
    return screens, session.log, injection_bytes


@pytest.fixture(scope="module", params=sorted(VARIANTS))
def variant(request, fake):
    cols, env = VARIANTS[request.param]
    screens, log, injection = _run_scenario(fake, cols, env)
    return request.param, cols, screens, log, injection


def _masked(name, lines):
    lines = normalize(lines)
    if name == "help":
        # Command and group counts move whenever a tool is added; the
        # layout is what this golden pins.
        lines = [re.sub(r"\d", "#", line) for line in lines]
    return lines


def test_golden_screens(variant):
    name, cols, screens, _log, _inj = variant
    for step, lines in screens.items():
        check_golden("%s__%s" % (step, name), _masked(step, lines))


def test_no_line_is_wider_than_the_terminal(variant):
    name, cols, screens, _log, _inj = variant
    for step, lines in screens.items():
        for line in lines:
            assert visible_width(line) <= cols - 1, (name, step, line)


def test_escape_free_variants_write_no_escape_byte(variant):
    name, _cols, _screens, log, _inj = variant
    if name not in ESCAPE_FREE:
        pytest.skip("colour variant")
    assert b"\x1b" not in log
    assert b"\x9b" not in log


def test_plain_mode_never_redraws_a_line(variant):
    name, _cols, _screens, log, _inj = variant
    if name not in NO_REDRAW:
        pytest.skip("colour variant redraws the live line by design")
    assert not re.search(rb"\r(?!\n)", log), "a bare carriage return redraws a line"


def test_injected_answer_is_inert(variant):
    _name, _cols, screens, _log, injection = variant
    for forbidden in (b"\x1b]52", b"\x07", b"\xc2\x9b", "‮".encode("utf-8"),
                      b"\x1b[2J"):
        assert forbidden not in injection, forbidden
    text = "\n".join(screens["injection"])
    assert "\\x1b]52" in text and "\\u202e" in text


def test_live_line_carries_phase_time_and_cancel_hint_and_no_json(variant):
    name, _cols, screens, _log, _inj = variant
    progress = "\n".join(screens["turn_progress"])
    assert "{" not in progress
    if name in ("dumb80", "plain80"):
        assert "working" in progress
        return
    assert re.search(r"working . routing . \d+s", progress), progress
    if name != "c50":
        assert "Ctrl-C cancels" in progress


def test_banner_is_short_and_never_advertises_shift_tab_on_posix(variant):
    _name, _cols, screens, _log, _inj = variant
    startup = [line for line in screens["startup"] if line.strip()]
    # identity, hint, status line, prompt
    assert len(startup) <= 4, startup
    assert "Shift+Tab" not in "\n".join(startup)
    assert "timestamp" not in "\n".join(startup)


def test_help_fits_an_80x24_screen(variant):
    name, _cols, screens, _log, _inj = variant
    if name != "c80":
        pytest.skip("80-column check")
    help_lines = screens["help"]
    # The echoed command, the help, a blank, the status line and the prompt.
    assert len(help_lines) <= 24, len(help_lines)
    legend = next(i for i, line in enumerate(help_lines) if "[asks]" in line)
    first_marked = next(i for i, line in enumerate(help_lines)
                        if line.rstrip().endswith(("[runs]", "[writes]", "[danger]", "[asks]"))
                        and i != legend)
    assert legend < first_marked


def test_elapsed_time_appears_once_per_finished_turn(variant):
    name, _cols, screens, _log, _inj = variant
    done = [line for line in screens["turn_done"] if "done " in line]
    assert len(done) == 1, done
    body = "\n".join(screens["turn_done"])
    assert "completed in" not in body


def test_unknown_command_is_one_line_with_the_suggestion_first(variant):
    name, _cols, screens, _log, _inj = variant
    lines = [line for line in screens["unknown"] if "unknown" in line]
    assert len(lines) == 1
    assert "did you mean /help?" in lines[0]


def test_declined_approval_names_the_diverted_command(variant):
    _name, _cols, screens, _log, _inj = variant
    text = "\n".join(screens["approval_declined"])
    assert "skipped" in text and "/runtime status" in text
    assert "/env was not run" in text
    assert "[asks]" in text


# --- single-session interaction checks ------------------------------------


@pytest.fixture()
def session(fake, tmp_path):
    fake._release.set()
    repl = ReplSession(tmp_path, fake.url, cols=80, env={"SONDER_REPL_HISTORY": "1"})
    repl.wait_prompt()
    yield repl
    repl.close()


def test_type_ahead_never_answers_an_approval(session):
    mark = session.mark()
    # "y" typed ahead of the prompt it would answer must be discarded.
    session.send(b"/runtime status\ry\r")
    session.wait_for(b"run it?", mark)
    session.settle(0.5)
    session.send(b"\r")
    session.wait_prompt(mark)
    text = "\n".join(session.screen(mark))
    assert "skipped" in text, text
    assert "runtime policy" not in text.lower() or "skipped" in text


def test_arrow_and_shift_tab_never_start_a_turn(session):
    mark = session.mark()
    session.send(b"\x1b[A\r")
    session.settle(1.0)
    session.send(b"\x1b[Z\r")
    session.settle(1.0)
    assert "working" not in session.text(mark)
    mark = session.mark()
    session.send(b"/nosuc\x1b[DX\r")
    session.wait_prompt(mark)
    # Commands are case-insensitive, so the edited "/nosuXc" is echoed lowered.
    assert "unknown  command /nosuxc" in "\n".join(session.screen(mark))


def test_ctrl_c_once_clears_twice_exits(session):
    mark = session.mark()
    session.send(b"half typed")
    session.send(b"\x03")
    session.wait_for(b"Ctrl-C again", mark)
    session.send(b"\x03")
    session.child.expect(pexpect.EOF, timeout=90)


def test_history_is_persisted_private_and_skips_credentials(session, tmp_path):
    for line in ("/help", "/login alice hunter2", "/status"):
        mark = session.mark()
        session.send_line(line)
        if line.startswith("/login"):
            session.wait_for(b"type 'yes' to run", mark)
            session.settle(0.3)
            session.send_line("n")
        session.wait_prompt(mark)
    path = tmp_path / "repl_history"
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600
    saved = path.read_text().splitlines()
    assert saved[-2:] == ["/help", "/status"]
    assert not any("hunter2" in line for line in saved)


def test_pass_with_nothing_to_rate_never_prompts(session):
    mark = session.mark()
    session.send_line("/pass")
    session.wait_prompt(mark)
    text = session.text(mark)
    assert "nothing to rate yet" in text
    assert "run it?" not in text
