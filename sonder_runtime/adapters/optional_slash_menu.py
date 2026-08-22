"""Optional discovery boundary for the interactive slash-menu helper."""
from __future__ import annotations

import importlib


def load_optional_slash_menu():
    try:
        return importlib.import_module(
            "sonder_runtime.adapters.optional_slash_menu_impl"
        )
    except ImportError:
        return None


__all__ = ["load_optional_slash_menu"]
