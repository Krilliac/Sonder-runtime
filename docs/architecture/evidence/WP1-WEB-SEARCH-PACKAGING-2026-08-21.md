# WP1 web-search packaging evidence — 2026-08-21

## Scope

This slice moves the public web-search provider policy out of the root
`web_tools.py` module and into the canonical
`sonder_runtime.adapters.web_search` adapter. The root module remains the
compatibility surface for the broader web-tools family, including the shared
pinned HTTP transport and fetch/weather helpers.

## Ownership result

- Canonical provider selection, fallback, relevance scoring, and result
  formatting: `sonder_runtime.adapters.web_search`.
- Shared lower-level transport and provider parsers: `web_tools.py`, retained
  intentionally because fetch and other web tools still use them.
- Root `web_tools.web_search` and `web_tools.format_search_results`: thin
  compatibility delegates; no search algorithm remains in the root module.

## Evidence

- `tests/test_web_tools.py` and existing web security/query suites preserve
  the legacy surface and security seams.
- `tests/test_web_search_adapter.py` covers consent and bounded adapter output.
- `tests/test_web_search_compatibility.py` proves root delegation and the
  packaged ownership ratchet.
- `scripts/check_architecture.py` rejects reintroducing a root `web_search`
  function and requires the packaged `search_raw` entrypoint.

## Verification

The focused web-search, web-tools security, compatibility, and architecture
tests passed on this branch. The architecture checker and compile check also
passed after the migration.
