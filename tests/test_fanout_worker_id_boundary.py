"""Boundary tests for _fanout_worker_id rewiring to domain function."""

import os
import threading

import server
from sonder_runtime.domain.fanout_worker_identity import (
    FANOUT_WORKER_INSTANCE,
    fanout_worker_id,
)


def test_domain_function_format():
    result = fanout_worker_id("abc123", 42, 99)
    assert result == "fanout-abc123-42-99"


def test_server_delegate_uses_domain_function():
    result = server._fanout_worker_id()
    expected = fanout_worker_id(
        FANOUT_WORKER_INSTANCE, os.getpid(), threading.get_ident(),
    )
    assert result == expected


def test_server_delegate_matches_format():
    result = server._fanout_worker_id()
    assert result.startswith("fanout-")
    parts = result.split("-", 1)
    assert len(parts) == 2
