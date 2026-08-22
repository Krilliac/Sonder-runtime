"""CLI interface (SPEC-5 §28)."""

from .extensions import ExtensionCommand
from .commands import RepositoryMapCommand

__all__ = ["ExtensionCommand", "RepositoryMapCommand"]
