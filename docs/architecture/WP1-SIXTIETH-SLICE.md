# WP1 Sixtieth Slice — narrow the Ollama lifecycle compatibility surface

## Scope

The production composition root already imports
`sonder_runtime.adapters.ollama_lifecycle`; the root `ollama_lifecycle.py`
module has no production callers. It remains as a compatibility path for
external imports and immutable checkout compatibility, but its wildcard
re-export has been narrowed to the two public lifecycle helpers:
`resident_models` and `cleanup_orphaned_discovery_probes`.

Private process-inspection, trust-root, and platform hooks are no longer
accidentally exposed through the root path. The packaged adapter declares the
same explicit `__all__` contract. Reloading the compatibility module preserves
identity with the packaged functions, and packaging continues to include the
root compatibility file through the normal source payload rules.

No command-catalog, persistence, launcher, HTTP/REPL, or strangler-services
paths were changed.

## Evidence

- Root caller audit: only `tests/test_ollama_lifecycle.py` imports the root
  compatibility path; `server.py` imports the packaged adapter directly.
- Focused lifecycle and server regression tests pass, including compatibility
  surface and reload coverage.
- `python -m compileall -q sonder_runtime server.py`: passed.
- `python scripts/check_architecture.py`: passed.
- `python scripts/check_requirement_evidence.py`: passed.
- Staged and working-tree whitespace checks: passed.

## Boundary decision

Deletion is not yet proven safe because the root path remains an intentional
external compatibility surface. The ratchet removes private API leakage while
preserving public imports and package behavior.
