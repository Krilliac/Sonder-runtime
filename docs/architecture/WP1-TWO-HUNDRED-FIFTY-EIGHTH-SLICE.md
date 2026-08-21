# WP1 Two-Hundred-Fifty-Eighth Slice — requested-context caller migration

## Boundary

Rewired the canonical session, structured-answer, and standard-answer paths
in `server.py` to invoke the packaged requested-context adapter directly. The
root `_context_requested` helper remains only as a compatibility delegate.
Tests that need a deterministic context now intercept the packaged adapter,
which is the canonical production seam.

## Evidence

- A source-level regression test proves that production code contains no call
  to the `_context_requested` compatibility wrapper.
- Context-selection, structured-answer, history, trace, server-helper, and
  model-fanout regressions pass: **247 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

The compatibility helper remains for existing external callers and tests.
This slice does not claim full root-module removal or formal checklist
completion.
