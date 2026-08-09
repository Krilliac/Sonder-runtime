"""Legacy adapter binding semantic recall to its migrated implementation."""
from __future__ import annotations

import importlib


class LegacyRecallGateway:
    def recall(self, connection, task, **options):
        implementation = importlib.import_module("sonder_runtime.adapters.recall")
        return implementation.recall(connection, task, **options)
