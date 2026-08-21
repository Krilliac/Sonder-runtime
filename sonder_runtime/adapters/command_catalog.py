"""Dynamic compatibility provider for the legacy command catalog."""
from __future__ import annotations

import importlib


def _catalog():
    return importlib.import_module("command_catalog")


class CommandCatalogProvider:
    def __getattr__(self, name):
        return getattr(_catalog(), name)


command_catalog = CommandCatalogProvider()

__all__ = ["CommandCatalogProvider", "command_catalog"]
