"""Ambient OperationContext: published by a surface, scoped, thread-isolated."""
import threading

import pytest

from sonder_runtime.application.context import (
    bind_operation_context,
    current_operation_context,
    local_owner_context,
)


def test_no_ambient_context_by_default():
    assert current_operation_context() is None


def test_bind_publishes_and_restores_nested_contexts():
    outer = local_owner_context(correlation_id="outer-1", source="http")
    inner = local_owner_context(correlation_id="inner-1", source="http")
    with bind_operation_context(outer) as bound:
        assert bound is outer
        assert current_operation_context() is outer
        with bind_operation_context(inner):
            assert current_operation_context() is inner
        assert current_operation_context() is outer
    assert current_operation_context() is None


def test_binding_is_unwound_when_the_block_raises():
    context = local_owner_context(correlation_id="boom-1")
    with pytest.raises(RuntimeError):
        with bind_operation_context(context):
            raise RuntimeError("turn failed")
    assert current_operation_context() is None


def test_only_operation_contexts_can_be_bound():
    with pytest.raises(TypeError):
        with bind_operation_context({"correlation_id": "not-a-context"}):
            pass


def test_ambient_context_does_not_leak_across_threads():
    seen = []
    context = local_owner_context(correlation_id="thread-owner")
    ready = threading.Event()
    release = threading.Event()

    def other_thread():
        ready.wait(5)
        seen.append(current_operation_context())
        release.set()

    worker = threading.Thread(target=other_thread)
    worker.start()
    with bind_operation_context(context):
        ready.set()
        release.wait(5)
    worker.join(5)
    assert seen == [None]
