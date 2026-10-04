#!/usr/bin/env python3
"""Measure bounded OpenRouter batching against a synthetic loopback HTTP peer.

No live credentials, external endpoint, model or persistent state is used.
Timing measures adapter/scheduler/HTTP overhead plus an authored peer delay;
it does not measure OpenRouter service latency or model quality.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonder_runtime.adapters.inference.openrouter_gateway import (  # noqa: E402
    OpenRouterCreditsExhausted, OpenRouterGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.ports.model_gateway import ModelRequest  # noqa: E402


class SyntheticPeer:
    def __init__(self, delay_ms: float, failure_every: int):
        self.delay = delay_ms / 1000
        self.failure_every = failure_every
        self.lock = threading.Lock()
        self.seen: Counter[str] = Counter()
        self.active = self.peak = 0

    def handle(self, handler):
        body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
        prompt = body["messages"][-1]["content"]
        with self.lock:
            self.seen[prompt] += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            index = int(prompt.rsplit(":", 1)[1])
            failed = self.failure_every > 0 and (index + 1) % self.failure_every == 0
            document = ({"error": {"code": 402, "message": "synthetic credits failure"}}
                        if failed else {
                            "id": "synthetic", "model": body["model"],
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": prompt}}],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                      "total_tokens": 2, "cost": 0},
                        })
            data = json.dumps(document).encode()
        finally:
            # Count concurrent peer work, before publishing a finished reply.
            with self.lock:
                self.active -= 1
        handler.send_response(402 if failed else 200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


def benchmark(*, batch_size=64, rounds=5, workers=(1, 2, 4, 8),
              delay_ms=10.0, failure_every=11):
    """Return timing and independent peer invariants; raise on lost/extra sends."""
    if (type(batch_size) is not int or not 1 <= batch_size <= 64
            or type(rounds) is not int or not 1 <= rounds <= 100
            or not workers or len(workers) > 8
            or any(type(n) is not int or not 1 <= n <= 8 for n in workers)
            or not math.isfinite(delay_ms) or not 0 <= delay_ms <= 100
            or type(failure_every) is not int or not 0 <= failure_every <= 64):
        raise ValueError("benchmark parameters exceed the bounded workload")
    peer = SyntheticPeer(delay_ms, failure_every)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            peer.handle(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    server.request_queue_size = 16
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.02}, name="batch-benchmark-peer")
    thread.start()
    rows = []
    before_threads = set(threading.enumerate())
    try:
        gateway = OpenRouterGateway(env={
            "SONDER_ALLOW_CLOUD": "1",
            "OPENROUTER_API_KEY": "sk-or-v1-" + "0123456789abcdef" * 4,
            "SONDER_OPENROUTER_BASE_URL": "http://127.0.0.1:%d/api/v1" % server.server_address[1],
            "SONDER_OPENROUTER_MODEL": "synthetic/batch",
        }, policy_models=None, request_admission=HostModelRequestAdmission())
        for scenario, count in enumerate(workers):
            elapsed = []
            successes = failures = 0
            with peer.lock:
                peer.peak = 0
            expected = []
            for iteration in range(rounds):
                prompts = ["%d:%d:%d" % (scenario, iteration, i) for i in range(batch_size)]
                expected.extend(prompts)
                requests = tuple(ModelRequest(prompt, "code") for prompt in prompts)
                started = time.perf_counter()
                outcomes = gateway.generate_batch(requests, local_owner_context(
                    correlation_id="synthetic-batch-stress", cloud_allowed=True,
                ), max_workers=count)
                elapsed.append(time.perf_counter() - started)
                if len(outcomes) != batch_size:
                    raise RuntimeError("batch lost outcomes")
                for i, (prompt, outcome) in enumerate(zip(prompts, outcomes, strict=True)):
                    failed = failure_every > 0 and (i + 1) % failure_every == 0
                    if failed:
                        if outcome.response is not None or not isinstance(outcome.error, OpenRouterCreditsExhausted):
                            raise RuntimeError("batch lost the expected per-item failure")
                        failures += 1
                    else:
                        if (outcome.error is not None or outcome.response.text != prompt
                                or outcome.response.tokens_in != 1 or outcome.response.tokens_out != 1):
                            raise RuntimeError("batch reordered or corrupted a successful sibling")
                        successes += 1
            with peer.lock:
                if any(peer.seen[prompt] != 1 for prompt in expected):
                    raise RuntimeError("physical sends were missing or retried")
                peak = peer.peak
            if peak > count:
                raise RuntimeError("peer work exceeded configured concurrency")
            rows.append({"workers": count, "requests": len(expected), "successes": successes,
                         "expected_failures": failures, "peak_peer_work": peak,
                         "elapsed_seconds": sum(elapsed),
                         "requests_per_second": len(expected) / sum(elapsed),
                         "median_batch_seconds": statistics.median(elapsed),
                         "max_batch_seconds": max(elapsed)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    leaked = [item.name for item in threading.enumerate() if item not in before_threads]
    if thread.is_alive() or leaked or peer.active:
        raise RuntimeError("benchmark did not drain its peer and workers")
    if sum(peer.seen.values()) != batch_size * rounds * len(workers):
        raise RuntimeError("peer observed unexpected physical sends")
    return {"synthetic": True, "python": sys.version.split()[0],
            "batch_size": batch_size, "rounds": rounds, "peer_delay_ms": delay_ms,
            "failure_every": failure_every, "scenarios": rows,
            "physical_sends": sum(peer.seen.values()), "drained": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--delay-ms", type=float, default=10)
    parser.add_argument("--failure-every", type=int, default=11)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    try:
        result = benchmark(batch_size=args.batch_size, rounds=args.rounds,
                           workers=tuple(args.workers), delay_ms=args.delay_ms,
                           failure_every=args.failure_every)
    except (ValueError, RuntimeError) as error:
        parser.exit(1, "batch benchmark failed: %s\n" % error)
    output = json.dumps(result, indent=2) + "\n"
    if args.json:
        args.json.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
