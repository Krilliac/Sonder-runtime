# OpenRouter batch stress qualification

Run from the repository root with its qualified Python environment:

```sh
python scripts/benchmark_openrouter_batch.py --rounds 5 --json /tmp/openrouter-batch.json
python scripts/benchmark_openrouter_batch.py --rounds 20 --json /tmp/openrouter-batch-soak.json
```

The harness starts a synthetic HTTP peer on an ephemeral loopback port and
uses the actual OpenRouter gateway, HTTP transport and owned batch scheduler.
It supplies its own fake credential and explicit environment snapshot; it
does not read a live API key, contact OpenRouter, load weights or open a state
database. Synthetic responses report zero cost and one input/output token.

The default workload sends 64 requests per batch, five rounds at each of
1, 2, 4 and 8 workers. Every eleventh request receives an ordinary synthetic
402 failure. The harness requires exactly one physical send per input,
ordered results with intact successful siblings, the expected error type,
bounded concurrent peer work and drained HTTP/worker threads after shutdown.
A failed invariant exits nonzero. Workload parameters have fixed ceilings.

The report includes throughput, median and maximum batch time and observed
peer concurrency. The peer's configurable delay is authored, so these
measurements cover adapter/scheduler/HTTP behavior on this host. They do not
establish live OpenRouter latency, model quality or a hardware-independent
performance threshold. Repeat comparisons on the same idle host and Python
version; compare identical workload parameters. Never use one noisy run to
increase production concurrency automatically.

## Initial local receipt

On 2026-10-04, Python 3.12.14 completed the default 1,280-send workload with
1,180 successful outcomes and 100 expected failures. All send-count, ordering,
concurrency and cleanup invariants passed. A single run with a 10 ms peer
delay measured 47.9, 83.6, 129.5 and 174.7 requests/second at 1, 2, 4 and 8
workers respectively. A subsequent 5,120-send soak also passed every invariant (4,720 successes and
400 expected failures). The listen backlog is configured before server activation;
independent regression coverage checks the actual activated backlog, successful
usage accounting and cleanup after validation failure. Concurrent builds and
browser tests make these local timings point estimates. The generated JSON
belongs outside the repository.

The normally collected test runs a small real HTTP workload and verifies
cleanup. The existing OpenRouter batch tests separately qualify cancellation,
deadlines, rate admission, privacy preferences, accounting and per-item
domain failures.
