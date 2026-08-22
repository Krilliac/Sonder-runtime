# EXT-003 bounded extension host evidence

Date: 2026-08-21

## Scope

Added a standard-library-only JSON-lines process boundary for extension code
at `sonder_runtime/adapters/extensions/host.py`. The host owns child-process
lifetime and enforces startup and call deadlines, per-line output bounds, and
bounded restart/crash recovery. Protocol failures are translated into typed
host errors and failed calls are not replayed. A requested `memory_limit_bytes`
is applied before the startup handshake through
`NativeExtensionMemoryLimiter`: Windows uses a Job Object process-memory cap
with kill-on-close; Linux uses `resource.prlimit` for a hard address-space
cap; platforms without a native adapter still fail closed. The limiter is
injectable so the lifecycle contract is tested without relying on process RSS.
The extension manifest also carries an optional typed, digest-bound
`resources.memory_limit_bytes` budget, and application/CLI/HTTP paths preserve
that declaration through durable registry state.

## Verification

Command:

`python -m pytest -q tests/test_extension_host.py tests/test_extension_memory_limits.py --basetemp <fresh-temp>`

Result: **9 passed, 1 skipped** (the unsupported-platform branch is skipped on
Windows; the live Windows Job Object attachment test is exercised there).
Also passed `python -m compileall -q sonder_runtime tests`,
`python scripts/check_architecture.py`, and `git diff --check`.

## Limitations

This is a bounded process/protocol and native-limit slice, not a claim that the
complete EXT-003 requirement is verified. Manifest admission and production registry wiring now have a
composition acceptance slice: `tests/production/test_extension_composition.py`
installs through the live application graph, reopens the SQLite-backed
registry through a fresh graph, rechecks the manifest digest, and preserves
the explicit disabled/unverified state when provenance is absent. Native
memory limiting is proven at the host seam on the available Windows runtime
and through a deterministic POSIX adapter test; the declared budget is persisted,
and a trusted persisted installation is exercised through the native host in
`test_persisted_healthy_installation_reaches_native_experiment_host`. The full
platform matrix remains unverified. Artifact verification evidence is now
typed, provenance-digest-bound, and persisted before a trusted installation
can enter the host boundary; discovery/download orchestration remains owned by
the existing artifact adapter.
