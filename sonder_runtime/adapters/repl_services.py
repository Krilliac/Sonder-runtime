"""Dynamic compatibility providers for non-core REPL helper modules."""
from __future__ import annotations

import importlib
from typing import Any


class ReplServiceProvider:
    def __init__(self, module_name: str) -> None:
        self.module_name = module_name

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module(self.module_name), name)


from .web_intents import web_intents
assetgen = ReplServiceProvider("assetgen")
personas = ReplServiceProvider("personas")
consult = ReplServiceProvider("consult")
tier_router = ReplServiceProvider("tier_router")
code_improve = ReplServiceProvider("code_improve")
command_router = ReplServiceProvider("command_router")
harness_tools = ReplServiceProvider("harness_tools")
project_scaffold = ReplServiceProvider("project_scaffold")
symbol_index = ReplServiceProvider("symbol_index")

__all__ = [
    "ReplServiceProvider", "assetgen", "code_improve", "command_router",
    "consult", "harness_tools", "personas", "project_scaffold",
    "symbol_index", "tier_router", "web_intents",
]
