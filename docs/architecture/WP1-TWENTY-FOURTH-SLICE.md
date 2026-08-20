# WP1 Twenty-Fourth Slice: Package Signed Updates

Status: implemented on `agent/wp1-execution-status`.

## Scope

The signed update service now lives at
`sonder_runtime.adapters.updates.service`. The package CLI and update engine,
download/activation, TUF publisher, manifest-trust, schema-guard, and update
tests use the packaged service. Root `sonder_updates.py` is retired.

The architecture policy records only the service-local optional boundaries:
`tuf` for signature verification and `web_tools` for the existing guarded
fetch helper.

## Evidence

- Update download, engine, TUF publisher, update, schema-guard, manifest-trust,
  and production architecture regression: **120 passed, 8 skipped**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 9.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The remaining roots are server, immutable autopilot/fleet aliases, migration
registry, lifecycle, serving, REPL, and update-engine entrypoint. The update
engine remains separate because it is the orchestration entrypoint around the
packaged service.
