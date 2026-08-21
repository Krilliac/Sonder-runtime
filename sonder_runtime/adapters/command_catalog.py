"""Compatibility provider for the legacy command catalog."""
from __future__ import annotations

import importlib
from typing import Any, Mapping, Optional, Sequence

from sonder_runtime.application.ports.command_catalog import (
    CatalogCommand,
    CatalogInvocation,
    CommandCatalog,
)


def _catalog():
    return importlib.import_module("command_catalog")


class CommandCatalogProvider(CommandCatalog):
    """Typed facade over the legacy module, with lazy import-cycle avoidance."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_catalog(), name)

    def catalog(self) -> Sequence[CatalogCommand]:
        return _catalog().catalog()

    def http_catalog(self) -> Sequence[CatalogCommand]:
        return _catalog().http_catalog()

    def http_slash_tools(self) -> Mapping[str, Sequence[str]]:
        return _catalog().http_slash_tools()

    def by_name(self, name: str) -> Optional[CatalogCommand]:
        return _catalog().by_name(name)

    def complete(self, prefix: str = "", **kwargs: Any) -> Sequence[CatalogCommand]:
        return _catalog().complete(prefix, **kwargs)

    def help_text(self, topic: str = "") -> str:
        return _catalog().help_text(topic)

    def parse_invocation(self, line: str) -> Optional[CatalogInvocation]:
        return _catalog().parse_invocation(line)


command_catalog = CommandCatalogProvider()

__all__ = ["CommandCatalogProvider", "command_catalog"]
