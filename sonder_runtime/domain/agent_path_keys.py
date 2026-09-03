"""Canonical path-key normalization for the agent run-created-paths ledger."""

from __future__ import annotations

import os.path


def created_path_key(path) -> str:
    """One canonical key per on-disk target.

    Case-folded and separator-normalized so ``src\\a.h``, ``src/a.h``, and
    ``SRC/a.h`` all name the same file on Windows.
    """
    return os.path.normcase(os.path.normpath(str(path or "")))
