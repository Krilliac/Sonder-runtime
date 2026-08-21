"""Packaged execution adapters and compatibility provider façades.

The concrete code runner lives in :mod:`.code_runner`.  The provider objects
remain here so existing server and interface patch points keep their identity
and lazy lookup behavior while ownership moves into the adapter layer.
"""
from __future__ import annotations

import importlib

from . import code_runner


class _RootProvider:
    module_name = ""

    def __getattr__(self, name):
        return getattr(importlib.import_module(self.module_name), name)


class GroundingProvider(_RootProvider):
    module_name = "grounding"


class CodeRunnerProvider(_RootProvider):
    module_name = "sonder_runtime.adapters.execution_tools.code_runner"


grounding = GroundingProvider()

__all__ = ["CodeRunnerProvider", "GroundingProvider", "code_runner", "grounding"]
