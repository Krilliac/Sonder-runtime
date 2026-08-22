# WP1 Two-Hundred-Sixty-Third Slice — core model-error adapter migration

## Boundary

Moved endpoint-target classification into
`sonder_runtime.adapters.model_error_formatting.format_runtime_model_call_error`
and rewired the canonical offload, grounded extraction, serialized-answer,
and standard-answer paths to use that adapter directly. The root
`_format_model_call_error` helper remains as a compatibility delegate.

## Evidence

- Adapter tests cover local, remote, and hosted target classification.
- An AST regression test proves the four core model paths contain no call to
  the root error-formatting wrapper.
- Model-error, retry, server-helper, fanout, offload-schema, and gateway
  regressions pass: **343 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Several secondary reporting and fanout receipt paths still use the root
compatibility delegate and are staged for later migration. This slice does not
claim full root-module removal or formal checklist completion.
