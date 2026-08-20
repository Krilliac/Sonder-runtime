# WP1 One-Hundred-Thirty-Eighth Slice

## Campaign environment-failure policy ownership

The pure `_campaign_environment_failure` policy now lives in the packaged
domain module `sonder_runtime.domain.campaign_environment`. The server keeps
an identity-compatible `_campaign_environment_failure` alias so campaign
callers and integrations retain their existing import surface.

The policy remains deterministic: only the explicit
`missing runtime/compiler:` sentinel is classified as a host-toolchain
failure; model output errors, timeouts, empty output, and `None` remain model
failures.

## Verification

- Focused campaign policy tests: 3 passed.
- Targeted compile of `server.py`, the new domain module, and its tests passed.
- Existing server helper regression: 1 passed (216 deselected).
- Repository-wide `python -m compileall -q sonder_runtime server.py` passed.
- Architecture, requirement-evidence, and `git diff --check` gates passed.
- No non-server adapters or prior migration files were changed by this slice.
