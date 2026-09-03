# WP1 Three-Hundred-Twenty-Ninth Slice — model catalog parsing

## Boundary

The parsing half of the Ollama catalog discovery family now lives in
`sonder_runtime/domain/model_catalog.py`: `catalog_names`,
`catalog_records`, `installed_records`, `catalog_revision` and
`resolve_record`, each taking the fetched payload or records and keeping the
case-insensitive de-duplication, the sort orders, the `:latest` candidate
rule and the digest lookup unchanged.

The root functions `discovered_models`, `discovered_model_records`,
`_runtime_installed_model_records`, `_cache_model_revision` and
`resolve_discovered_model_record` remain in `server.py` as thin delegates
that keep the `_get("/api/tags")` fetch, the empty-selector short-circuits
and the catalog-outage fallback exactly where they were, so the `_get`,
`discovered_model_records`, `resolve_discovered_model_record` and
`_cache_model_revision` monkeypatch seams keep working.
`resolve_discovered_model` is unchanged.

## Evidence

- `tests/test_model_catalog_boundary.py` verifies de-duplication, ordering and empty payloads for all three parsers, revision and resolution rules, the root delegates fetching once through the domain, and their short-circuits and failure shapes.
- `python -m pytest -q tests/test_model_catalog_boundary.py tests/test_model_fanout.py tests/test_runtime_policy_server.py tests/test_server_helpers.py -k 'boundary or discover or catalog or revision or fanout or runtime'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
