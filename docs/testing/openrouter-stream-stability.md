# OpenRouter stream stability qualification

Run offline from the repository root:

```sh
python -m pytest -q tests/test_openrouter_stream_backpressure.py tests/test_openrouter_stream_socket_boundary.py
python -m pytest -q tests/test_openrouter_gateway.py tests/test_openrouter_batch.py
```

The focused controls use an injected synthetic SSE iterator and a real
ephemeral loopback HTTP peer, with a fake credential and explicit cloud
consent. They contact no provider, load no weights and establish no model
quality or live OpenRouter throughput. The peer emits 2,048 Unicode chunks.
A paused consumer must retain at most 64 delivery entries; close, cancellation
and deadline expiry must unblock its worker without another physical send.
Normal drain preserves all text in order, emits final usage once, and keeps
the privacy-first provider object. Error controls preserve earlier chunks and
deliver the terminal exception, including when the backlog is full. An empty
completion still raises the existing no-usable-text domain error.

A separate actual HTTP control flushes one chunk and gates every later byte.
It verifies that iterator close returns while the socket-blocked worker is
still alive; releasing the peer then drains that worker without another send.
This tests the documented cooperative boundary rather than claiming that
iterator close interrupts an arbitrary socket read synchronously.

On the unchanged implementation, the backlog and control tests failed:
the queue admitted the full response and cancellation/deadline checks were
skipped while queued text was available. The first test draft also wrongly
expected an empty completion to succeed; that fixture was corrected to
preserve established output validation. Baseline and candidate logs are kept
outside the repository, separately from qualified revision receipts.

The fixed bound is a chunk-count bound. The gateway still assembles full
response text for evidence and accounting, bounded by the actual HTTP
transport's existing response ceiling. Iterator close waits at most 250 ms;
a blocked socket read remains subject to its transport timeout. Explicitly
close abandoned iterators. These controls prove local buffering, lifecycle,
ordering and privacy behavior, not remote cancellation or billing cessation.
