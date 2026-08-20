# WP1 one-hundred-twenty-fourth slice — schema-gap policy ownership

This slice moves the pure `_format_schema_gaps` helper out of `server.py` and
into `sonder_runtime.domain.schema_policy`. The server keeps an
identity-preserving compatibility alias while all existing schema-validation
callers retain their output wording, ordering, and bounded disclosure.

The packaged policy accepts explicit gap data only. It has no transport,
filesystem, persistence, or server-state dependency, so it can be tested
directly at the domain boundary.

## Verification

- `python -m pytest tests/test_schema_policy.py tests/test_offload_schema.py tests/test_grounded_extraction.py` — focused schema and integration tests passed.
- `python scripts/check_architecture.py` — passed with zero violations.
- `python scripts/check_requirement_evidence.py` — passed.
- The focused Python compile gate passed.
- `git diff --check` passed for this slice's files.

This is migration evidence, not a master-spec checkbox credit. No commit or
push was made for this slice.
