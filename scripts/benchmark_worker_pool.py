#!/usr/bin/env python3
"""Measure OllamaWorkerPool scheduling overhead in isolation.

The pool sits on every local inference round trip, so its per-request cost has
to stay invisible next to even the fastest model call (tens of milliseconds).
This harness drives ``request()`` with a no-op sender -- no sockets, no model
-- so the number it prints is the scheduler itself: selection, telemetry, and
lock traffic. Run it before and after touching the pool.

    python scripts/benchmark_worker_pool.py
    python scripts/benchmark_worker_pool.py --requests 200000 --threads 8

Offline by construction: worker origins are loopback literals and the sender
never opens a connection.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool  # noqa: E402

_ORIGINS = tuple("http://127.0.0.%d:11434" % octet for octet in range(1, 5))


def _run(pool: OllamaWorkerPool, requests: int, threads: int) -> float:
    """Return wall seconds to push ``requests`` no-op calls through the pool."""
    def worker(count: int) -> None:
        for _ in range(count):
            pool.request(lambda origin: origin)

    started = time.perf_counter()
    if threads <= 1:
        worker(requests)
    else:
        share, remainder = divmod(requests, threads)
        pool_threads = [
            threading.Thread(target=worker, args=(share + (1 if i < remainder else 0),))
            for i in range(threads)
        ]
        for thread in pool_threads:
            thread.start()
        for thread in pool_threads:
            thread.join()
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=1)
    arguments = parser.parse_args()

    scenarios = [
        ("1 worker (pool disabled path shape)", OllamaWorkerPool(_ORIGINS[0])),
        ("4 workers, least-inflight", OllamaWorkerPool(_ORIGINS[0], _ORIGINS[1:])),
        (
            "4 workers, latency tie-break [experimental]",
            OllamaWorkerPool(_ORIGINS[0], _ORIGINS[1:], latency_aware=True),
        ),
    ]
    print(
        "%d requests, %d thread(s), python %s"
        % (arguments.requests, arguments.threads, sys.version.split()[0])
    )
    for label, pool in scenarios:
        # Warm once so first-call setup is not billed to the scenario.
        pool.request(lambda origin: origin)
        elapsed = _run(pool, arguments.requests, arguments.threads)
        print(
            "  %-45s %8.0f req/s  %6.2f us/request"
            % (label, arguments.requests / elapsed, elapsed / arguments.requests * 1e6)
        )
        total = sum(snapshot.requests for snapshot in pool.snapshots())
        if total != arguments.requests + 1:
            print(
                "  COUNTER MISMATCH: telemetry saw %d of %d requests"
                % (total, arguments.requests + 1),
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
