"""Optional discovery boundary for the interactive slash-menu helper."""
from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType


def load_optional_slash_menu() -> ModuleType | None:
    try:
        return importlib.import_module(
            "sonder_runtime.adapters.optional_slash_menu_impl"
        )
    except ImportError:
        return None


__all__ = ["load_optional_slash_menu"]
