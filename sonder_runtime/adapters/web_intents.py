"""Packaged compatibility boundary for conservative web intent routing.

The legacy classifier remains a compatibility engine while callers migrate;
the runtime-facing provider is lazy so importing the REPL never evaluates the
legacy module or any network capability.
"""
from __future__ import annotations

import importlib


class WebIntentProvider:
    _module_name = "web_intents"

    def __getattr__(self, name: str):
        return getattr(importlib.import_module(self._module_name), name)


web_intents = WebIntentProvider()

__all__ = ["WebIntentProvider", "web_intents"]
