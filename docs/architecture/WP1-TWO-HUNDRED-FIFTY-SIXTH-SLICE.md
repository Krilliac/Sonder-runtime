# WP1 Two-Hundred-Fifty-Sixth Slice — native-context caller migration

## Boundary

Rewired the canonical trace metadata paths in `server.py` to invoke the
packaged native-context selection adapter directly. The root `_context_native`
helper remains only as a compatibility delegate for existing callers and
tests.

## Evidence

- A source-level regression test proves that production code contains no call
  to the `_context_native` compatibility wrapper.
- Context-selection, trace, server-helper, and model-fanout regressions pass:
  **235 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

The separate requested-context compatibility helper still has production
callers and is intentionally staged for a later slice. This change does not
claim full root-module removal or formal checklist completion.
