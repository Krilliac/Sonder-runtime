"""The offline stress harness checks real HTTP work, failures and cleanup."""
import threading

import pytest

from scripts.benchmark_openrouter_batch import benchmark
from scripts import benchmark_openrouter_batch as harness


def test_loopback_stress_preserves_every_ordered_outcome_and_drains():
    before = set(threading.enumerate())
    report = benchmark(batch_size=4, rounds=2, workers=(1, 2), delay_ms=1, failure_every=2)
    assert report["synthetic"] is True
    assert report["drained"] is True
    assert report["physical_sends"] == 16
    assert all(row["successes"] == row["expected_failures"] == 4 for row in report["scenarios"])
    assert report["scenarios"][0]["peak_peer_work"] == 1
    assert set(threading.enumerate()) <= before


@pytest.mark.parametrize("options", [
    {"rounds": 101}, {"batch_size": 65}, {"workers": (9,)},
    {"delay_ms": float("nan")}, {"failure_every": -1},
])
def test_unbounded_workload_refused_before_starting_peer(options):
    before = set(threading.enumerate())
    with pytest.raises(ValueError):
        benchmark(**options)
    assert set(threading.enumerate()) == before


def test_peer_listens_with_the_configured_backlog(monkeypatch):
    backlogs = []
    activate = harness.ThreadingHTTPServer.server_activate

    def capture_activation(server):
        backlogs.append(server.request_queue_size)
        return activate(server)

    monkeypatch.setattr(harness.ThreadingHTTPServer, "server_activate", capture_activation)
    benchmark(batch_size=2, rounds=1, workers=(2,), delay_ms=0, failure_every=0)
    assert backlogs == [16]


def test_usage_accounts_only_successful_responses(monkeypatch):
    usage = []
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.openrouter_gateway.record_usage", usage.append,
    )
    report = benchmark(batch_size=4, rounds=2, workers=(1, 2), delay_ms=0, failure_every=2)
    assert len(usage) == sum(row["successes"] for row in report["scenarios"]) == 8
    assert all(item["cost_usd"] == 0 for item in usage)
    assert sum(item["prompt_tokens"] for item in usage) == 8
    assert sum(item["completion_tokens"] for item in usage) == 8


def test_peer_and_workers_drain_when_a_completed_batch_fails_validation(monkeypatch):
    before = set(threading.enumerate())
    generate = harness.OpenRouterGateway.generate_batch

    def complete_then_fail(*args, **kwargs):
        generate(*args, **kwargs)
        raise RuntimeError("ordinary benchmark validation failure")

    monkeypatch.setattr(harness.OpenRouterGateway, "generate_batch", complete_then_fail)
    with pytest.raises(RuntimeError, match="ordinary benchmark validation failure"):
        benchmark(batch_size=4, rounds=1, workers=(2,), delay_ms=1, failure_every=2)
    assert set(threading.enumerate()) <= before
