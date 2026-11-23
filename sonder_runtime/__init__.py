"""Single module entry point for the Sonder runtime (SPEC-2 WP1).

``python -m sonder_runtime <command>`` is the supported way to run every
production operation.  The historical launch scripts remain as
compatibility surfaces and will delegate here.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the repository root and its legacy ``runtime/`` and ``scripts/`` module
# directories importable when the package is executed from any working dir.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (
    str(_REPO_ROOT / "scripts"),
    str(_REPO_ROOT / "runtime"),
    str(_REPO_ROOT),
):
    if _path not in sys.path:
        sys.path.insert(0, _path)
