# ARCH-011 root-ratchet evidence — 2026-08-21

## Change

The architecture checker’s root compatibility baseline now contains exactly
the one live root dependency: `server`. The previously historical
`autopilot_store` and `fleet_store` entries were stale after their production
callers moved behind packaged adapters and were removed from the baseline.

This is a shrink-only policy change. The checker still rejects any growth of
`ROOT_LEGACY_MODULES` beyond the baseline and its limit, while immutable
migration imports remain separately documented compatibility exceptions.

## Evidence

- `scripts/check_architecture.py`: `BASELINE_ROOT_LEGACY_MODULES == {"server"}`.
- `tests/production/test_architecture.py`: exact baseline assertion and
  shrink-only ratchet test.
- `python scripts/check_architecture.py`: expected exit 0 with no violations.
- No runtime, session, or API files are changed by this slice.
