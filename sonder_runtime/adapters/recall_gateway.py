"""Semantic recall gateway adapter."""
from __future__ import annotations

import importlib
import sqlite3

from ..domain.common.errors import DependencyUnavailable


class LegacyRecallGateway:
    def recall(self, connection, task, **options):
        implementation = importlib.import_module("sonder_runtime.adapters.recall")
        try:
            return implementation.recall(connection, task, **options)
        except (OSError, sqlite3.Error) as exc:
            raise DependencyUnavailable("semantic recall storage is unavailable") from exc
