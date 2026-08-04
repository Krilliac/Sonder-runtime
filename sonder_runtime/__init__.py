"""Single module entry point for the Sonder runtime (SPEC-2 WP1).

``python -m sonder_runtime <command>`` is the supported way to run every
production operation.  The historical launch scripts remain as
compatibility surfaces and will delegate here.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The runtime is still a flat repository-root monolith (SPEC-3 moves it
# into packages).  Make the repository root importable when the package is
# executed from an installed or nested location.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
