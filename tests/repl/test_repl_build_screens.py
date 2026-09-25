"""Golden screens for ``/build`` and ``/fix-build`` in the real REPL (pty).

``_repl_launch_build`` puts a fake build service behind the typed gateway;
the slash chain, the console permission gate and its approval prompt, and
the build facade are production code. Steps: a read (``/build model``) runs
with no prompt; ``/fix-build`` shows the execution-graded approval prompt,
which is answered ``y``; the result is laid out with the notice/table/footer
components. Each step's screen is compared with
``goldens/<step>__<variant>.txt`` (``SONDER_UPDATE_GOLDENS=1`` rewrites).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")
pytest.importorskip("pyte")

from tests.repl import pty_harness  # noqa: E402
from tests.repl.fake_ollama import FakeOllama  # noqa: E402
from tests.repl.pty_harness import (  # noqa: E402
    ReplSession, check_golden, normalize, visible_width,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not hasattr(pexpect, "spawn"), reason="needs a POSIX pty"),
]

LAUNCH = Path(__file__).resolve().parent / "_repl_launch_build.py"
VARIANTS = {
    "c80": (80, {}),
    "c50": (50, {}),
    "nocolor80": (80, {"NO_COLOR": "1"}),
}


@pytest.fixture(scope="module")
def fake():
    server = FakeOllama()
    yield server
    server.close()


def _run_scenario(fake, cols, env):
    home = tempfile.mkdtemp(prefix="sonder-repl-build-")
    fake._release.set()
    original = pty_harness.LAUNCH
    pty_harness.LAUNCH = LAUNCH
    try:
        session = ReplSession(home, fake.url, cols=cols, env=env)
    finally:
        pty_harness.LAUNCH = original
    screens = {}
    try:
        session.wait_prompt()

        mark = session.mark()
        session.send_line("/build model")
        session.wait_prompt(mark)
        screens["build_model"] = session.screen(mark)

        mark = session.mark()
        session.send_line("/fix-build game --config Debug")
        session.wait_for(b"run it?", mark)
        session.settle(0.3)
        screens["fix_build_approval"] = session.screen(mark)
        mark = session.mark()
        session.send_line("y")
        session.wait_prompt(mark)
        screens["fix_build_result"] = session.screen(mark)
    finally:
        session.close()
    return screens, session.log


@pytest.fixture(scope="module", params=sorted(VARIANTS))
def variant(request, fake):
    cols, env = VARIANTS[request.param]
    screens, log = _run_scenario(fake, cols, env)
    return request.param, cols, screens, log


def test_golden_build_screens(variant):
    name, _cols, screens, _log = variant
    for step, lines in screens.items():
        check_golden("%s__%s" % (step, name), normalize(lines))


def test_build_screens_fit_the_terminal(variant):
    name, cols, screens, _log = variant
    for step, lines in screens.items():
        for line in lines:
            assert visible_width(line) <= cols - 1, (name, step, line)


def test_the_read_runs_without_a_prompt_and_the_fix_is_asked_as_execution(variant):
    _name, _cols, screens, _log = variant
    model = "\n".join(screens["build_model"])
    assert "run it?" not in model and "approve" not in model
    assert "cmake" in model and "targets (3)" in model
    approval = "\n".join(screens["fix_build_approval"])
    assert "approve" in approval and "/fix-build game --config Debug" in approval
    assert "[runs]" in approval
    result = "\n".join(screens["fix_build_result"])
    # the stand-in gateway answers only a console-decided (gate=surface) request
    assert "NOT_A_CONSOLE_DECISION" not in result
    assert "build_fix_status" in result and "running" in result
    assert "done" in result


def test_no_color_writes_no_escape_byte(variant):
    name, _cols, _screens, log = variant
    if name != "nocolor80":
        pytest.skip("colour variant")
    assert b"\x1b" not in log
