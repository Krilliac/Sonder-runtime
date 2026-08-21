# WP1 Two-Hundred-Fifty-Seventh Slice — local option caller migration

## Boundary

Rewired the remaining canonical local-model request, cache-key, vision,
offload, and fanout-synthesis paths in `server.py` to invoke the packaged
`environment_options.local_model_options` policy directly. The root
`_local_model_options` helper remains only as a compatibility delegate.

## Evidence

- A source-level regression test proves that production code contains no call
  to the `_local_model_options` compatibility wrapper.
- The offload context-window regression now intercepts the packaged policy and
  confirms the computed native context is passed through.
- Local option, runtime summary, server-helper, model-fanout, and vision
  regressions pass: **47 passed** in the focused slice.
- `git diff --check` and the architecture gate pass.

## Limitation

The root compatibility helper remains for existing external callers and
tests. This slice does not claim full root-module removal or formal checklist
completion.
