# WP1 Three-Hundred-Sixteenth Slice — serve-target selection policy

## Boundary

The serve-target selection predicates (`_allow_cloud_fallback_for_target`,
`_explicit_serve_selection`) now live in
`sonder_runtime/domain/serve_selection.py` as `allow_cloud_fallback_for_target`
and `explicit_serve_selection`, unchanged. `server.py` keeps both root names
as identity-preserving alias imports, so the serve-target resolver and the
availability-fallback path call the same objects.

## Evidence

- `tests/test_serve_selection_boundary.py` verifies the alias identities, the fallback rule for configured tiers versus exact model selectors, and explicit selection by named target or non-default tier.
- `python -m pytest -q tests/test_serve_selection_boundary.py tests/test_server_helpers.py -k 'boundary or fallback or selection or serve'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
