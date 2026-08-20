"""Error translation for repository-backed adapter calls.

Repository implementations may expose storage-specific failures. The
application boundary must expose the stable Sonder error taxonomy instead.
Keeping that translation here gives repository adapters one reusable seam
without making the domain/application layers import SQLite or ``OSError``.
"""
from __future__ import annotations

import sqlite3

from ..domain.common.errors import DependencyUnavailable, InvalidInput, NotFound


def call_repository_operation(operation, *args, **kwargs):
    """Run a repository operation and translate its storage failures."""
    try:
        return operation(*args, **kwargs)
    except ValueError as exc:
        if str(exc).startswith("no unique task '"):
            raise NotFound(str(exc)) from exc
        raise InvalidInput(str(exc)) from exc
    except (OSError, sqlite3.Error) as exc:
        raise DependencyUnavailable(str(exc)) from exc


__all__ = ["call_repository_operation"]
