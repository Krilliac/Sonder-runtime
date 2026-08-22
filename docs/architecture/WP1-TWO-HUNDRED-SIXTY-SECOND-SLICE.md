# WP1 Two-Hundred-Sixty-Second Slice — master-timeout caller migration

## Boundary

Rewired the canonical master-agent and audit-worker orchestration paths in
`server.py` to invoke the packaged master-timeout policy directly. The root
`_master_timeout` helper remains only as a compatibility delegate.

## Evidence

- A source-level regression test proves production code contains no call to
  the master-timeout compatibility wrapper.
- Master-timeout, timeout-propagation, orchestration-memory, and continuable
  subagent regressions pass: **16 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Other root model-error and cloud-policy delegates remain staged for later
migration. This slice does not claim formal checklist completion.
