# WP1 Two-Hundred-Thirteenth Slice — schema response parsing ownership

## Boundary

Moved the pure leading-JSON-object parser used by schema-constrained model
responses from root `server.py` into the packaged domain schema policy. The
root helper remains as a compatibility boundary and translates the domain
`ValueError` into the existing protocol `ModelCallError`; response-footers and
error wording remain compatible.

This slice is limited to the previously unmigrated `_leading_json_object`
helper and does not alter the ownership boundaries completed through slice
212.

## Evidence

- `tests/test_schema_policy.py` verifies first-value decoding, object-only
  validation, and the root protocol-error compatibility contract.
- `python -m pytest tests/test_schema_policy.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
