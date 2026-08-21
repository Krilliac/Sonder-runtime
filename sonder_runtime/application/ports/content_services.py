"""Ports for bounded training and content classification helpers."""
from __future__ import annotations

from typing import Any, Protocol


class TrainingTasks(Protocol):
    def sample(self, count: int) -> Any: ...


class FeedbackClassifier(Protocol):
    def classify_signal(self, content: str) -> Any: ...
    def classify_feedback(self, content: str) -> Any: ...


class IntentClassifier(Protocol):
    def classify(self, content: str) -> Any: ...
    def containment_egress_refusal(self, content: str) -> Any: ...
    def classify_execution(self, content: str) -> Any: ...


__all__ = ["FeedbackClassifier", "IntentClassifier", "TrainingTasks"]
