# WP1 one-hundred-twenty-fifth slice — legacy model gateway ownership

This slice moves the `LegacyModelGateway` implementation out of the generic
`sonder_runtime.adapters.strangler_services` module into the canonical
`sonder_runtime.adapters.legacy_model_gateway` adapter. The strangler module
retains an identity-preserving import alias, so existing callers and tests
continue to use the same class contract.

The adapter still lazily calls the unchanged legacy `server.sonder` and
embedding entry points. Request shaping, response validation, context
cancellation/deadline checks, and embedding order are unchanged. No
`server.py`, bootstrap, repository, tool, or event-sink files were modified.

## Verification

- `python -m pytest tests/test_legacy_model_gateway_adapter.py tests/test_legacy_model_gateway.py tests/test_model_gateway_conformance.py -q` — focused gateway tests passed.
- `python scripts/check_architecture.py` — passed with zero violations.
- `python scripts/check_requirement_evidence.py` — passed.
- The focused Python compile gate passed.
- `git diff --check` passed for this slice's files.

This is migration evidence, not a master-spec checkbox credit. No commit or
push was made for this slice.
