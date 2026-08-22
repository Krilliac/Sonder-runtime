"""Provider-neutral types for the command catalog boundary.

This module deliberately contains no adapter or legacy-root imports.  The
packaged provider and the source-derived root catalog can therefore agree on
the shape of the boundary without making either implementation import the
other at runtime.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class CatalogParam(Protocol):
    name: str
    type: str
    required: bool
    default: Any


@runtime_checkable
class CatalogCommand(Protocol):
    name: str
    aliases: Sequence[str]
    tool: str
    category: str
    risk: str
    summary: str
    params: Sequence[CatalogParam]
    native: bool


CatalogInvocation = tuple[str, dict[str, Any]]


@runtime_checkable
class CommandCatalog(Protocol):
    def catalog(self) -> Sequence[CatalogCommand]: ...
    def http_catalog(self) -> Sequence[CatalogCommand]: ...
    def http_slash_tools(self) -> Mapping[str, Sequence[str]]: ...
    def by_name(self, name: str) -> Optional[CatalogCommand]: ...
    def complete(self, prefix: str = "", **kwargs: Any) -> Sequence[CatalogCommand]: ...
    def help_text(self, topic: str = "") -> str: ...
    def parse_invocation(self, line: str) -> Optional[CatalogInvocation]: ...


__all__ = [
    "CatalogCommand", "CatalogInvocation", "CatalogParam", "CommandCatalog",
]
