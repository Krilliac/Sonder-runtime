from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.repository_errors import call_repository_operation
from sonder_runtime.adapters.task_repository import _store_call
from sonder_runtime.domain.common.errors import DependencyUnavailable, InvalidInput, NotFound


def test_task_repository_uses_canonical_repository_error_adapter():
    assert _store_call is call_repository_operation


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("no unique task 'x'"), NotFound),
        (ValueError("invalid status"), InvalidInput),
        (sqlite3.Error("database unavailable"), DependencyUnavailable),
        (OSError("storage unavailable"), DependencyUnavailable),
    ],
)
def test_repository_error_adapter_translates_storage_failures(error, expected):
    def operation():
        raise error

    with pytest.raises(expected, match=str(error)):
        call_repository_operation(operation)


def test_repository_error_adapter_returns_operation_result():
    assert call_repository_operation(lambda value: value + 1, 41) == 42
