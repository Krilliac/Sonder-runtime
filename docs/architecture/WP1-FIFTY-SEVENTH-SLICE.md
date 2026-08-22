# WP1 Fifty-Seventh Slice — Typed Runtime Model Configuration

## Change

The import-time model/tier seed formerly assembled inline in `server.py` now
has a pure package-domain projection in
`sonder_runtime.domain.runtime_model_configuration`. The frozen
`RuntimeModelConfiguration` captures the stable alias, local/cloud defaults,
retired-cloud fallback set, and initial tier bindings.

## Contract preserved

Defaults and environment overrides are unchanged, including the retired
`qwen3-coder:480b-cloud` fallback and the optional empty reasoning/vision
bindings. `server.py` retains its existing names (`TIERS`, `CLOUD_TIERS`,
`LOCAL_TIERS`, and the model constants) as compatibility aliases. The live
`TIERS` dictionary remains mutable because runtime-policy refresh is an
intentional in-process behavior; the new projection only owns its immutable
import-time seed.

## Boundary

The projection reads no process environment itself: callers pass an explicit
mapping. It imports only standard-library types and remains below the
composition root. It does not reach command catalog, persistence, launchers,
HTTP/REPL, or strangler services.

## Evidence

- Focused projection tests cover defaults, environment overrides, retired
  cloud fallback, immutability, and compatibility seed isolation.
- Compilation, architecture, requirement-evidence, and staged/working diff
  checks pass.
