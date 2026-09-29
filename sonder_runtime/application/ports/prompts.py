"""Prompt port: render a named, operator-editable prompt.

The adapter (``sonder_runtime.adapters.prompt_store.render``) reads the
shipped Markdown default or an operator override, fresh each turn. Application
services take the renderer as an injected dependency so they never touch the
filesystem themselves.
"""
from __future__ import annotations

from typing import Protocol


class PromptRenderer(Protocol):
    def __call__(self, name: str, /, **fields: object) -> str: ...
