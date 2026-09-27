"""Paths and a deterministic environment for one REPL test process.

This module has no terminal dependencies, so the piped (non-pty) REPL
contracts can use it on every platform. ``pty_harness`` re-exports these
names for the pseudo-terminal screen tests, which additionally need
``pexpect`` and ``pyte`` (POSIX only).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDENS = Path(__file__).resolve().parent / "goldens"
LAUNCH = Path(__file__).resolve().parent / "_repl_launch.py"

# A loopback port nothing listens on, so the banner's endpoint reads the
# same everywhere ("not listening").
CLOSED_PORT = "1"


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
