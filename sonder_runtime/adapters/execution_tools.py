"""Dynamic compatibility providers for grounded execution helpers."""
from __future__ import annotations

import importlib


class _RootProvider:
    module_name = ""

    def __getattr__(self, name):
        return getattr(importlib.import_module(self.module_name), name)


class GroundingProvider(_RootProvider):
    module_name = "grounding"


class CodeRunnerProvider(_RootProvider):
    module_name = "code_runner"


grounding = GroundingProvider()
code_runner = CodeRunnerProvider()

__all__ = ["CodeRunnerProvider", "GroundingProvider", "code_runner", "grounding"]
