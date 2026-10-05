# OpenRouter stream stability qualification

Run offline from the repository root:

```sh
python -m pytest -q tests/test_openrouter_stream_backpressure.py tests/test_openrouter_stream_socket_boundary.py
python -m pytest -q tests/test_openrouter_stream_terminal.py
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

The terminal controls use the real HTTP transport and scratch test-session
storage. EOF before `[DONE]` must raise `DependencyUnavailable` after earlier
deltas drain, without a normal final chunk. This includes close-delimited
responses, a body shorter than its declared Content-Length, a full backlog,
EOF without any deltas and EOF after a finish/usage event. Failure capture and
the content-free provider observer must report the dependency failure.
Positive controls preserve optional finish reasons when `[DONE]` is present, explicit error and
malformed-event behavior, and once-only accounting for a completed stream
whose later evidence persistence fails. An incomplete stream can still be
billable; these controls establish no remote billing or cancellation facts.

On baseline `207be7a2604a1a5d99590fe5fa95973777ae8cdb`, five
incomplete-stream controls returned normal completions. The zero-delta EOF
control instead failed later output validation, after recording a successful
provider response. All six failed the expected terminal contract; the four
original positive/error controls and completed-response capture-failure
control passed.

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

## Error detail sanitation controls

The sanitation controls use configured dummy values and real ephemeral
loopback peers with explicit consent. Messages must be redacted before the
240-character display bound. A generic HTTP error's optional type is also
redacted, retaining its 64-character input/output bound. Benign detail and
typed errors remain meaningful; malformed fields are filtered.

The 17 new controls cover 32- and 384-character configured values, HTTP/SSE
display-boundary errors, benign and Unicode details, type/limit compatibility,
and valid near-limit error payloads. The two boundary-stream cases require
one correlated provider request and failure, the allowlisted dependency code,
a content-free failed observer record, and no configured dummy value in stored
payloads or observer facts. Earlier deltas remain ordered, without a normal
final chunk or completed usage. Existing DONE, consent, cooldown, batching,
accounting and cooperative-close controls remain covered.

In the offline qualification on 2026-10-05, all 17 new controls passed
(with 34 other cases deselected), and the broader focused selection passed
170 tests with no failures, errors or skips. The separate stress control also
passed: 120 independent loopback exchanges, 40 each for generic HTTP type,
HTTP boundary/long-value errors, and SSE errors after 128 Unicode deltas.
At most two exchanges and two submitted futures are active. Each exchange
makes exactly one physical POST, without a retry, and raises the expected
sanitized domain failure. All peers and stream workers drain; delivery queues
retain the 64-entry bound.

The separate synthetic batch regression recorded 128 physical sends across
one- and two-worker scenarios: 120 successes and eight expected failures
(four per scenario), with complete drain. These workloads exercise local
privacy, accounting, ordering and lifecycle contracts using dummy data;
no provider is contacted and no weights are loaded. Remote throughput, model
quality, cancellation and billing cessation remain outside their scope.

Run offline from the repository root:

```sh
python -m pytest -q tests/test_openrouter_gateway.py -k 'error_kind_redacts or error_message_redacts or stream_error_redacts or error_detail_compatibility or error_sanitation_size_controls'
python -m pytest -q tests/test_openrouter_gateway.py::test_error_sanitation_small_stress
```

The size controls stay below the existing 16 KiB HTTP error-body read bound
and 1 MiB SSE-line ceiling; the 16 MiB total stream bound is unchanged.
Redaction precedes display truncation on those bounded inputs.

One fresh-process warmup was excluded from the following three serial
measured repetitions. Both controls and all setup/call/teardown phases passed
in each process. Test-call timings include loopback transport, redaction and
assertions; fixture setup and teardown have separate phase records.

| Size control | Repetitions 1, 2, 3 (ms) | Min, median, max (ms) |
| --- | --- | --- |
| HTTP error-body budget | 14.290, 14.466, 14.379 | 14.290, 14.379, 14.466 |
| SSE line budget | 231.512, 234.988, 241.096 | 231.512, 234.988, 241.096 |

| Measured process | Session wall (s) | Process CPU (s) | Peak RSS (MiB) |
| --- | --- | --- | --- |
| 1 | 2.686181 | 1.908199 | 155.426 |
| 2 | 2.701029 | 1.926189 | 155.426 |
| 3 | 2.641583 | 1.873550 | 155.426 |

Wall and CPU measurements span the pytest session-start to session-finish
hooks, including collection and fixture phases. CPU is the pytest process's
own CPU time. Peak RSS is the Linux pytest process's lifetime high-water mark sampled at
session finish, including imports and fixtures; its acceptance is checked
after the process exits. It covers the pytest process itself and does not
enforce a live memory limit. Individual samples and min/median/max
are reported without a tail percentile or a new timing threshold.

The complete local qualification passed all 26 stages. The full suite recorded
25,490 passed tests, 335 skips and four passed subtests in 763.07 seconds, with
no failures or errors. All 335 skip reasons remain visible in the raw report;
no skip was converted into a pass. The 74 required native controls passed:
70 PowerShell controls, three Go controls and one AF_UNIX control. The separate
TUF stage passed 30 tests with zero skips.

Architecture, requirement/evidence, error, lint, documentation, history and
smoke gates passed. Final source and preservation guards were unchanged;
owned fixture directories were removed and owned child processes were reaped
without signals. The history ratchet recorded seven known historical items
and zero unexpected items. A clean-history release remains unqualified.

The independent raw audit verified all 247 retained artifacts, exact stage
commands and exits, native identities, every skip, measured samples, guards
and cleanup. Logs, JUnit XML and detailed phase records are retained outside
Git. Content-free receipt identifiers are:

- Local qualification SHA256: `1985798552d75711487c9a771f7c7dc4956d07b478fb0137362234ca646838c6`.
- Independent raw audit SHA256: `132b5b18afc37bb91b28c9af67628a435403cc7530f2b13de0bea3f0833d94fc`.
- Qualified gateway SHA256: `92495d14d6a0ffa31630188370823100bd874dee987ff8c22b5fed6d1249ac73`.
- Qualified test-module AST SHA256: `f9c111f0c7545841f35a1cce06e4bac3b877b8f04fbf3e0b87d7fd9fc266e8ce`.

The subsequent test-comment cleanup preserved the complete module AST.
Before merging, the final PR revision must also pass the required hosted checks.
