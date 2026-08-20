# WP1 One-Hundred-Forty-Eighth Slice

## Boundary

Moved the pure `_fanout_declares_generative_capability` catalog policy from
`server.py` into `sonder_runtime.domain.fanout_policy.declares_generative_capability`.
The server retains an identity-compatible alias, so existing fanout callers
and tests keep the established compatibility surface while the explicit
generation-capability decision now belongs to the domain boundary.

## Verification

- Added focused domain-policy coverage for positive, nested, non-generative,
  unknown, null, and compatibility-alias cases.
- Existing fanout policy and server fanout regressions passed.
- Architecture, requirement-evidence, compile, and `git diff --check` gates
  passed.

This slice changes only the server helper seam, the existing pure fanout domain
policy, focused tests, and this evidence note; no adapters or prior migration
files were modified.
