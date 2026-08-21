# WP1 Two-Hundred-Sixty-First Slice — location-consent caller migration

## Boundary

Rewired the canonical chat-web location-consent default path in `server.py`
to invoke the packaged location-consent policy directly. The root
`_env_location_consent` helper remains only as a compatibility delegate.

## Evidence

- A source-level regression test proves production code contains no call to
  the location-consent compatibility wrapper.
- Location-consent, chat-web routing, master-timeout, and timeout-propagation
  regressions pass: **42 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Other root policy delegates remain staged for later migration. This slice does
not claim full root-module removal or formal checklist completion.
