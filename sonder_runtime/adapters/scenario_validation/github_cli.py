"""GitHub CLI boundary; no policy decisions belong here."""

from __future__ import annotations

import subprocess
from typing import Callable


class GhCliAdapter:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.runner = runner or subprocess.run

    def run(self, command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return self.runner(command, capture_output=True, text=True, check=False, shell=False, timeout=30)
