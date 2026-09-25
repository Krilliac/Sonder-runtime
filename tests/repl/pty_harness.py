"""Drive the real REPL in a pseudo-terminal and read its screen.

``ReplSession`` spawns ``sonder repl`` (through ``_repl_launch``, which only
pins source-provenance facts) under ``pexpect`` with a chosen terminal size
and environment, against :class:`fake_ollama.FakeOllama`, in a throwaway
``SONDER_HOME``.  Every byte the REPL writes is kept in ``log``; a step's
screen is the ``pyte`` rendering of the bytes written since a mark, on a
fresh screen, so each golden shows exactly one step.

Goldens live in ``tests/repl/goldens``.  ``SONDER_UPDATE_GOLDENS=1``
rewrites them instead of comparing.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import time

import pexpect
import pyte

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = Path(__file__).resolve().parent / "goldens"
LAUNCH = Path(__file__).resolve().parent / "_repl_launch.py"

# A loopback port nothing listens on, so the banner's endpoint reads the
# same everywhere ("not listening").
CLOSED_PORT = "1"
_ESC = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def base_env(home, fake_url, **extra):
    """A minimal, deterministic environment for one REPL process."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "SONDER_HOME": str(home),
        "OLLAMA_HOST": fake_url,
        "SONDER_PORT": CLOSED_PORT,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "xterm-256color",
        "SONDER_REPL_HISTORY": "0",
    }
    for key in ("SONDER_TEST_DB_ROOT", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env.update({k: v for k, v in extra.items() if v is not None})
    for key in [k for k, v in extra.items() if v is None]:
        env.pop(key, None)
    return env


class ReplSession:
    """One interactive REPL in a pty of ``cols`` x ``rows``."""

    def __init__(self, home, fake_url, cols=80, rows=24, env=None, timeout=90):
        self.cols, self.rows = cols, rows
        self.log = b""
        self.env = base_env(home, fake_url, **(env or {}))
        self.child = pexpect.spawn(
            sys.executable, [str(LAUNCH)], env=self.env, cwd=str(ROOT),
            dimensions=(rows, cols), timeout=timeout,
        )
        self.timeout = timeout

    # -- reading -----------------------------------------------------------

    def _pump(self, seconds):
        try:
            chunk = self.child.read_nonblocking(65536, timeout=seconds)
        except pexpect.TIMEOUT:
            return False
        except pexpect.EOF:
            raise
        self.log += chunk
        return True

    def mark(self):
        return len(self.log)

    def wait_for(self, needle, start=0, timeout=None):
        """Read until ``needle`` (bytes or compiled regex) appears after ``start``."""
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            segment = self.log[start:]
            found = needle.search(segment) if hasattr(needle, "search") else needle in segment
            if found:
                return True
            if time.monotonic() > deadline:
                raise AssertionError("timed out waiting for %r; got:\n%s" % (
                    needle, self.text(start)))
            try:
                self._pump(0.2)
            except pexpect.EOF:
                raise AssertionError("REPL exited while waiting for %r:\n%s" % (
                    needle, self.text(start)))

    def settle(self, quiet=0.6):
        """Read until the REPL has written nothing for ``quiet`` seconds."""
        while True:
            try:
                if not self._pump(quiet):
                    return
            except pexpect.EOF:
                return

    def wait_prompt(self, start=0, timeout=None):
        """Wait for the next idle prompt after ``start``, then settle."""
        pattern = re.compile(
            rb"(?:\xe2\x9d\xaf|>)(?:\x1b\[[0-9;]*m)* (?:\x1b\[[0-9;?]*[a-zA-Z])*$")
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            self.settle(0.4)
            if pattern.search(self.log[start:]):
                return
            if time.monotonic() > deadline:
                raise AssertionError("no prompt; got:\n%s" % self.text(start))
            try:
                self._pump(0.3)
            except pexpect.EOF:
                raise AssertionError("REPL exited:\n%s" % self.text(start))

    # -- writing -----------------------------------------------------------

    def send_line(self, text):
        self.child.send(text.encode("utf-8") + b"\r")

    def send(self, data):
        self.child.send(data)

    def close(self):
        try:
            if self.child.isalive():
                self.child.sendcontrol("d")
                self.child.expect(pexpect.EOF, timeout=15)
        except Exception:
            pass
        finally:
            try:
                self.child.close(force=True)
            except Exception:
                pass

    # -- rendering ---------------------------------------------------------

    def text(self, start=0):
        return self.log[start:].decode("utf-8", "replace")

    def screen(self, start=0, rows=200):
        """The step's screen: bytes since ``start`` on a fresh terminal."""
        screen = pyte.Screen(self.cols, rows)
        stream = pyte.ByteStream(screen)
        stream.feed(self.log[start:])
        lines = [line.rstrip() for line in screen.display]
        while lines and not lines[-1]:
            lines.pop()
        return lines


def normalize(lines):
    """Mask what legitimately varies between runs: seconds and durations."""
    out = []
    for line in lines:
        line = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s)\b", "<t>", line)
        line = re.sub(r"\b\d+m \d{2}s\b", "<t>", line)
        out.append(line)
    return out


def check_golden(name, lines):
    """Compare ``lines`` with ``goldens/<name>.txt`` (or rewrite it)."""
    path = GOLDENS / ("%s.txt" % name)
    text = "\n".join(lines) + "\n"
    if os.environ.get("SONDER_UPDATE_GOLDENS") == "1" or not path.exists():
        if os.environ.get("SONDER_UPDATE_GOLDENS") != "1" and os.environ.get("CI"):
            raise AssertionError("missing golden %s" % path)
        GOLDENS.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert text == expected, "screen %s differs from its golden:\n--- got\n%s--- want\n%s" % (
        name, text, expected)


def visible_width(line):
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in line)


def strip_escapes(data):
    return _ESC.sub(b"", data)


__all__ = [
    "ReplSession", "base_env", "check_golden", "normalize", "strip_escapes",
    "visible_width",
]
