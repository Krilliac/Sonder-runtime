"""Pure operator-facing rendering of the runtime source recovery stash.

The Git adapter owns the stash itself; this module renders its status and
the outcome of a save or pop without echoing changed paths or stash prose.
It is explicit-input and side-effect free. Moved from ``server.py`` in the
WP1 Three-Hundred-Eleventh Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations


def format_stash(data, *, action="status"):
    """Render recovery state without echoing changed paths or stash prose."""
    if action == "status":
        return "\n".join((
            "Sonder source recovery stash:",
            "  checkout: %s" % ("clean" if data.get("clean") else "dirty"),
            "  changes: %s" % data.get("change_count", 0),
            "  recovery stashes: %s" % data.get("stash_count", 0),
            "  commands: /stash save | /stash save-untracked | /stash pop",
        ))
    before = data.get("before") or {}
    after = data.get("after") or {}
    if not data.get("changed"):
        return "runtime source stash: checkout already clean; no stash created"
    if action.startswith("save"):
        return "runtime source stash: saved changes; checkout is now %s" % (
            "clean" if after.get("clean") else "not clean",
        )
    return "runtime source stash: restored top recovery stash; checkout is now %s" % (
        "clean" if after.get("clean") else "dirty",
    )
