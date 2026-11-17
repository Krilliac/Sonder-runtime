"""Model discovery union and busy-primary catalog handling.

Tests ``pool_model_catalog.discovery_tags`` and ``OllamaWorkerPool`` catalog
methods:
- Pool union (``host_origin=None``) reads all routable members, skipping
  open-circuit workers and handling an empty union cleanly.
- Host-scoped reads (``host_origin=LOCAL``) read the primary directly when it
  is an admissible member, fall back to ``get_tags`` when the primary is
  unreachable (connection refused/reset, ``WorkerPoolUnavailable``) or not a
  member, and NEVER fall back to a worker catalog when the primary is merely
  busy (``TimeoutError``, ``GpuAdmissionRefused``, ``WorkerPoolBackpressure``).
"""
from __future__ import annotations

import logging
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.gpu_admission import GpuAdmissionRefused
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerPoolBackpressure,
    WorkerPoolDraining,
    WorkerPoolUnavailable,
)
from sonder_runtime.adapters.inference.pool_model_catalog import discovery_tags
from sonder_runtime.domain.gpu_admission import RefusalReason, Refused

LOCAL = "http://127.0.0.1:11434"
NODE1 = "https://node1.example:8443"
CATALOGS = {
    LOCAL: {
        "models": [
            {"name": "kimi-k3:cloud", "capabilities": ["completion"]},
            {"name": "shared:latest", "capabilities": ["completion"]},
        ]
    },
    NODE1: {
        "models": [
            {"name": "qwen3.6:35b", "capabilities": ["completion"]},
            {"name": "shared:latest", "capabilities": ["embedding"]},
        ]
    },
}


def test_discovery_tags_union_includes_busy_member_models():
    now = [1000.0]
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True, clock=lambda: now[0])
    assert pool.enabled
    result = discovery_tags(
        pool,
        lambda: CATALOGS[NODE1],
        lambda origin: CATALOGS[origin],
    )
    names = {row["name"] for row in result["models"]}
    assert names == {"kimi-k3:cloud", "shared:latest", "qwen3.6:35b"}
    shared = next(row for row in result["models"] if row["name"] == "shared:latest")
    assert shared["capabilities"] == ["completion"]


def test_discovery_tags_union_skips_open_circuit_worker_and_handles_empty_union():
    now = [1000.0]
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True, clock=lambda: now[0])
    with pool._condition:
        for state in pool._states:
            state.cooldown_until = now[0] + 60.0
    seen = []
    result = discovery_tags(
        pool,
        lambda: CATALOGS[LOCAL],
        lambda origin: seen.append(origin) or CATALOGS[origin],
    )
    assert seen == []
    assert result == {"models": []}


def test_discovery_tags_host_origin_reads_primary_directly():
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)
    called = []
    result = discovery_tags(
        pool,
        lambda: (_ for _ in ()).throw(AssertionError("should not call pooled get_tags")),
        lambda origin: called.append(origin) or CATALOGS[origin],
        host_origin=LOCAL,
    )
    assert called == [LOCAL]
    assert result == CATALOGS[LOCAL]


@pytest.mark.parametrize(
    "unreachable",
    [
        ConnectionRefusedError("refused"),
        ConnectionResetError("reset"),
        URLError("primary offline"),
        WorkerPoolUnavailable("primary unavailable"),
    ],
)
def test_discovery_tags_falls_back_and_logs_only_when_primary_unreachable(
    unreachable, caplog
):
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)

    def fetch(origin):
        if origin == LOCAL:
            raise unreachable
        return CATALOGS[origin]

    with caplog.at_level(logging.WARNING):
        result = discovery_tags(
            pool,
            lambda: CATALOGS[NODE1],
            fetch,
            host_origin=LOCAL,
        )
    assert result == CATALOGS[NODE1]
    assert any("falling back to pooled read" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "busy_error",
    [
        TimeoutError("primary timed out"),
        URLError(TimeoutError("socket timed out")),
        GpuAdmissionRefused(Refused(RefusalReason.HOST_RAM_FLOOR, "ram busy")),
        WorkerPoolBackpressure("pool busy"),
        WorkerPoolDraining("pool draining"),
    ],
)
def test_discovery_tags_never_falls_back_to_worker_when_primary_busy(busy_error):
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)

    def fetch(_origin):
        raise busy_error

    with pytest.raises(type(busy_error)):
        discovery_tags(
            pool,
            lambda: (_ for _ in ()).throw(AssertionError("must not fall back to worker")),
            fetch,
            host_origin=LOCAL,
        )


def test_discovery_tags_uses_pooled_read_when_host_not_a_pool_member():
    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)
    with pool._condition:
        pool._states[0].compatibility_error = "mismatch"
    assert pool.catalog_member(LOCAL) is False
    result = discovery_tags(
        pool,
        lambda: CATALOGS[NODE1],
        lambda _origin: (_ for _ in ()).throw(AssertionError("inadmissible host must not be read")),
        host_origin=LOCAL,
    )
    assert result == CATALOGS[NODE1]
