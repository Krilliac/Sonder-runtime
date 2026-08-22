"""Compatibility import for the historical evaluation-history module name."""

from .evaluation_history_reader import EvaluationHistoryReaderAdapter


# Preserve the public legacy name and exact class identity for existing users.
LegacyEvaluationHistoryReader = EvaluationHistoryReaderAdapter

__all__ = ["LegacyEvaluationHistoryReader"]
