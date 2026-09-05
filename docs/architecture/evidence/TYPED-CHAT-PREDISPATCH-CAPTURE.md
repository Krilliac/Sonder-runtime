# Typed chat: request durability before dispatch

This is bounded evidence for part of SESSION-004, SESSION-005, and SESSION-007.
It does not verify those requirements across every inference entrypoint.

## Behavior

Previously, `ChatService.complete` invoked the model before resolving session
capture. A provider exception or process exit could leave no durable request.
With capture configured, it now calls `SessionCaptureService.begin_request`
before invoking the gateway. The committed `model.requested` snapshot and
`user.message` share a request ID and turn ID. Each invocation gets a new request
ID; this is not an idempotent submission API.

A successful model result appends a correlated `model.response`. A model exception
appends `model.failed` with request ID, turn ID, and an allowlisted domain error
code. Raw exception text is not persisted in that event. Cancellation and deadline
failure terminate the attempt without declaring the entire session cancelled.
Unexpected exceptions record `INTERNAL_FAILURE`; the original exception still
propagates when failure capture succeeds.

Admission storage failure prevents dispatch. A failure to store a successful
response remains a persistence failure and never invokes the model again. A
failure to store a provider failure raises `IntegrityFailure`. Abrupt process exit
leaves an unresolved request; diagnosis does not retry it or invent a terminal.

The recorded request is the application-level `ModelRequest`. Provider-side
augmentation and effective wire-request capture remain separate migration work.
Explicitly constructed chat services without capture still operate without a
database. Production bootstrap supplies the existing lazy capture factory.

## Recovery and compatibility

Terminal events with explicit operation IDs close only the matching operation in
the same family. Unmatched explicit IDs and ambiguous legacy terminals make the
stream inconsistent. Legacy terminals without operation IDs can close a unique
same-family/same-turn operation, or a unique same-family operation if they also
lack turn identity. Legacy `tool.call` is recognized as an in-flight tool event.

`model.failed` contributes one operational error, never an assistant message.
Existing `capture_turn` callers remain retrospective; their responses now also
carry request ID. No database schema, stored history, hash chain, permission
grant, provider route, or installed runtime is migrated by this change.

A disposable database written by the new split-phase APIs was reopened using the
baseline checkout at `9338622651bfc88fb80c0bd09e5949b9e8b1081c`. Both successful and
failed requests passed old-reader integrity/replay/repair, with identical stored
event hashes. The failure remained outside the assistant transcript. This tests
the baseline session reader, not a complete old deployment or its error counters.

## Reproduction

Base: `9338622651bfc88fb80c0bd09e5949b9e8b1081c`. Verification environment:
Windows, CPython 3.12.10, repository-pinned MCP 2.0.0, isolated checkout and venv.
Tests use SQLite fixtures and fake gateways; no live model or remote worker.

```text
python -m pytest -q tests/test_chat_session_capture.py tests/test_session_split_capture.py tests/test_chat_crash_durability.py tests/test_session_repair.py tests/test_session_replay.py tests/test_remaining_session_durable_replay.py tests/test_live_session_capture.py tests/test_session_capture_once.py tests/test_loop_session_facade.py tests/production/test_session_continuity_wiring.py
python -m pytest -q tests/test_session_application_integration.py
python scripts/select_regression_tests.py --since 9338622651bfc88fb80c0bd09e5949b9e8b1081c --format args
```

The focused set passed **83 tests** after the legacy-tool compatibility fix. It
includes second-connection visibility during gateway execution, actual
`os._exit(91)` in a child process, normal-exit control, production bootstrap,
failure storage, identity matching, and unchanged transcript semantics. The new
ordering, correlation, and legacy-tool regressions were observed failing before
their respective fixes. Later integration and complete-suite results follow below.

The initial diff-selected set, run with `scripts/profile_tests.py --since` the base
above, passed **207 tests**. The selector is an iteration aid, not proof that all
changed behavior is covered. Independent task and whole-change reviews found no
remaining blocking issue after the legacy-tool fix.

The first complete Windows run at `a3fdff90` reported **12,470 passed, 57 failed,
59 skipped**. It exposed an existing integration fixture asserting the superseded
contract that provider failures leave no events. Updating that fixture to assert
the failed attempt, correlated identities, unchanged success prefix and reopened
six-event stream gives **84 passing focused tests** including that fixture.

Rerunning all 57 original failure nodes on unchanged base and the corrected tree
gave **49 failed, 8 passed on both**, with the same failed node IDs. The 49 include
Windows SQLite cleanup, shell/process and path-case failures. Seven other initial
failures passed in both targeted reruns, indicating larger-suite ordering or
environment sensitivity. This comparison is not a green full-suite claim.

At runtime/test revision `59d88deda2cb5c5172cb17abdf3346f275122fdb`, the combined
diff-selected suite passed **318 tests**, including a separate child-resume state
correction. Its focused child suite passed **30 tests**. The final complete run
(`python -m pytest -q --tb=short -n 2 --dist loadfile`) reported **12,493 passed,
53 failed, 59 skipped**, with four subtests passed. An unchanged-base complete
run with the same command/environment reported **12,412 passed, 63 failed,
59 skipped**, with four subtests passed. JUnit failed-node comparison found
**zero changed-only failures**: all 53 final failures also failed on baseline.
These runs expose existing Windows and order-sensitive failures; the difference
in counts is not evidence that this change fixed unrelated tests. Neither full
suite is green, and remote CI remains a separate gate.

Architecture, evidence-ledger, error-signal, doc-link and history-privacy gates
passed after the change. Both offline golden-evaluation lanes passed during
validation. History privacy retains seven known baseline debts; passing means
no new debt. This does not establish a clean history or remote CI success.

## Remaining scope

This change does not complete all MCP/HTTP/agent generation paths, durable tool
receipts, resumable standalone coding runs, interactive child-agent sessions,
distributed reservations, automatic coordinator takeover, or model sharding.
It proves committed request visibility and recovery after process exit, not
machine power-loss behavior or replication durability.
