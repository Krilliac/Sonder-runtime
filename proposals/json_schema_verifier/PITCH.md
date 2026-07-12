# json_schema_verifier

Validates a JSON string against a minimal schema (types + required keys, with
recursive `properties`/`items` for nested objects and arrays) using only the
stdlib `json` module -- no external tool, model, or GPU involved. It returns
the same `Verdict(passed, reason, detail)` shape used throughout `verifiers.py`,
with `detail` listing every violation (not just the first) so a repair loop
gets a full diagnostic in one pass instead of fixing one field at a time.

It's valuable wherever Sonder Runtime (or any agent it drives) is asked to emit
structured JSON -- tool-call arguments, config/manifest files, API response
shapes, the very `spec` dicts this repo's verifiers consume -- and needs a fast,
deterministic, zero-dependency oracle instead of an `llm_judge` call for a
check that's actually mechanical.

To integrate: add one line to `verifiers.py`'s `REGISTRY` --
`"json_schema": json_schema_verify` (after `from json_schema_verifier import
json_schema_verify` or copying the ~90-line module in) -- and call it via
`verifiers.verify("json_schema", artifact, {"schema": {...}})` from
`solver.solve_verified()` or `game_ladder` exactly like any other backend; no
changes to the solve/repair loop are needed since it is verifier-agnostic.
