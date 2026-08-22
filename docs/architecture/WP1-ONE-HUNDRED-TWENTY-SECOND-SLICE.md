# WP1 one-hundred-twenty-second slice — memory repository adapter ownership

This slice moves the stateful `LegacyMemoryRepository` implementation out of
the generic `sonder_runtime.adapters.strangler_services` module into the
canonical `sonder_runtime.adapters.memory_repository.MemoryRepositoryAdapter`.
The unit-of-work now constructs the packaged adapter directly. The old name is
retained only as an identity-preserving compatibility alias for callers that
still import it from `strangler_services`.

The adapter remains deliberately thin: it owns the connection-bound
`MemoryRepository` port while the existing memory-store and recall behavior,
including required outcome evidence, remains unchanged.

## Verification

- `python -m pytest tests/test_legacy_memory_repository.py
  tests/test_strangler_services_paths.py tests/production/test_composition_root.py`
  — 21 passed.
- `python scripts/check_architecture.py` — passed with zero violations.
- `python scripts/check_requirement_evidence.py` — passed.
- The focused Python compile gate passed.
- `git diff --check` passed for this slice's files.

This is migration evidence, not a master-spec checkbox credit. No `server.py`
or server-worker files are part of this slice.
