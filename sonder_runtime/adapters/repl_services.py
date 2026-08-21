"""Dynamic compatibility providers for non-core REPL helper modules."""
from __future__ import annotations

import importlib


class ReplServiceProvider:
    def __init__(self, module_name: str) -> None:
        self.module_name = module_name

    def __getattr__(self, name):
        return getattr(importlib.import_module(self.module_name), name)


from .web_intents import web_intents
personas = ReplServiceProvider("personas")
consult = ReplServiceProvider("consult")
tier_router = ReplServiceProvider("tier_router")
code_improve = ReplServiceProvider("code_improve")
command_router = ReplServiceProvider("command_router")
project_scaffold = ReplServiceProvider("project_scaffold")

__all__ = [
    "ReplServiceProvider", "code_improve", "command_router", "consult",
    "personas", "project_scaffold", "tier_router", "web_intents",
]
