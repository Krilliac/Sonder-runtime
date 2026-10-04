"""The offline stress harness checks real HTTP work, failures and cleanup."""
import threading

import pytest

from scripts.benchmark_openrouter_batch import benchmark


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
