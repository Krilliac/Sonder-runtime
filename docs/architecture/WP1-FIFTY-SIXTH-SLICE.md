# WP1 Fifty-Sixth Slice — Learning-Tier Presentation Boundary

## Change

The pure presentation portion of `server.learn_tiers()` now lives in
`sonder_runtime.adapters.learning_tier_formatting`. The composition root still
resolves the live tier mapping, learning selection, and cloud policy, then
passes those values into the adapter.

## Contract preserved

Tier order, local/cloud classification, disabled-cloud behavior, and the policy
guidance lines are unchanged. The adapter performs no I/O and imports neither
the server, model gateway, persistence, nor command catalog.

## Evidence

- Focused learning-tier formatter tests pass.
- Compilation, architecture, requirement-evidence, and staged/working diff
  checks pass.

## Boundary status

This slice removes one pure model/provider presentation loop from the
composition root. Tier resolution and policy decisions remain in `server.py`.
