"""Dynamic compatibility providers for training and content helpers."""
from __future__ import annotations

import importlib


class _RootProvider:
    module_name = ""

    def __getattr__(self, name):
        return getattr(importlib.import_module(self.module_name), name)


class TrainingTasksProvider(_RootProvider):
    module_name = "training_tasks"


class FeedbackClassifierProvider(_RootProvider):
    module_name = "feedback"


class IntentClassifierProvider(_RootProvider):
    module_name = "intents"


training_tasks = TrainingTasksProvider()
feedback = FeedbackClassifierProvider()
intents = IntentClassifierProvider()

__all__ = [
    "FeedbackClassifierProvider", "IntentClassifierProvider",
    "TrainingTasksProvider", "feedback", "intents", "training_tasks",
]
