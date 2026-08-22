# WP1 One-Hundred-Sixty-Second Slice

## Boundary moved

Moved Ollama model-root path resolution (`model_roots` and its duplicate-path
normalization helper) from the storage inspection adapter into the canonical
platform `model_paths` module. The storage adapter retains an identity-preserving
compatibility export, while OS storage classification and throughput probing stay
owned by `sonder_runtime.adapters.storage`.

## Evidence

- `tests/test_model_paths_platform.py` verifies the new platform owner, the
  compatibility export, non-creating configured paths, and normalization.
- `tests/test_sonder_storage.py` continues to cover storage behavior through the
  adapter surface.
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime server.py`
