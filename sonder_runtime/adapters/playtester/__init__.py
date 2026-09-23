"""Concrete playtest and GitHub boundary adapters."""

from .github_cli import GhCliAdapter
from .process import ProcessAdapter

__all__ = ["GhCliAdapter", "ProcessAdapter"]
