# WP1 One-Hundred-Second Slice — canonical configuration ownership

`sonder_runtime.platform.config` now owns the complete typed, fail-closed
configuration implementation. The root `sonder_config.py` file remains only
as an external-tooling and legacy-caller compatibility surface, re-exporting
the canonical classes, loader, constants, and `ConfigError` without creating
a second implementation.

The move preserves TOML, secrets-file, environment, and command-line
precedence; safe defaults; validation aggregation; security checks; redacted
serialization; and exact object/exception identity. The canonical module now
resolves the default home through `sonder_runtime.platform.paths`. The
security-sensitive `unsafe_lab` dependency remains an explicit platform
boundary dependency and is not duplicated or relaxed by this slice.

Focused ownership and configuration regressions are covered by
`tests/test_config_ownership.py` and the existing production configuration,
HTTP boundary, doctor, preflight, and entrypoint suites.
