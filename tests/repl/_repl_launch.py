"""Launch the real ``sonder repl`` with the volatile banner facts pinned.

The screen goldens must not change when the checkout falls behind
origin/main or a new commit lands, so the source-provenance fields the
banner and ``/about`` read are fixed here.  Everything else -- composition,
the permission gate, the model path (against ``fake_ollama``) -- is the
production code, unpatched.
"""

from __future__ import annotations

import sys

from sonder_runtime.interfaces.repl import repl as _repl

_original_banner_state = _repl._banner_state


def _pinned_banner_state(*args, **kwargs):
    state = _original_banner_state(*args, **kwargs)
    state.behind = 0
    state.restart_required = False
    state.update_state = "current"
    state.installed_commit = state.running_commit = state.newest_commit = "0123456789ab"
    state.installed_time = state.newest_time = "2026-01-01T00:00:00+00:00"
    state.session_id = "session-fixed"
    return state


_repl._banner_state = _pinned_banner_state


if __name__ == "__main__":
    from sonder_runtime.__main__ import main

    sys.exit(main(["repl", *sys.argv[1:]]))
