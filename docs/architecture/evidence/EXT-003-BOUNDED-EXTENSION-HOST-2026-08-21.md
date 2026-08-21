# EXT-003 bounded extension host evidence

Date: 2026-08-21

## Scope

Added a standard-library-only JSON-lines process boundary for extension code
at `sonder_runtime/adapters/extensions/host.py`. The host owns child-process
lifetime and enforces startup and call deadlines, per-line output bounds, and
bounded restart/crash recovery. Protocol failures are translated into typed
host errors and failed calls are not replayed.

## Verification

Command:

`python -m pytest -q tests/test_extension_host.py --basetemp .pytest-extension-host-final`

Result: **6 passed**. Also passed `python -m compileall -q sonder_runtime tests`,
`python scripts/check_architecture.py`, and `git diff --check`.

## Limitations

This is a bounded process/protocol slice, not a claim that the complete
EXT-003 requirement is verified. The current host does not enforce a native
memory limit, manifest admission, or production extension registry wiring;
those remain separate implementation work.
