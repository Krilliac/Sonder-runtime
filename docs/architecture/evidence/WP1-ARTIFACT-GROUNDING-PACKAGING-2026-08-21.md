# WP1 artifact-grounding packaging checkpoint

Date: 2026-08-21  
Scope: `artifact_grounding.py`, packaged artifact adapters, direct server and
artifact-generation references, related tests, architecture ratchets, and
evidence ledger.

## Ownership result

The canonical deterministic artifact validator now lives at
`sonder_runtime/adapters/artifact_grounding.py`. It retains requirement
parsing, recipe inference, bounded file and bundle validation, risk and
grounding checks, and human-readable formatting. The root
`artifact_grounding.py` is an identity compatibility redirect, preserving
legacy imports, private validation helpers, and monkeypatch seams.

`server.py` and `assetgen.py` import the packaged adapter directly. The
architecture checker ratchets the root module as a compatibility boundary and
rejects production callers that import the root implementation.

## Verification

Focused command:

```text
python -m pytest -q tests/test_artifact_grounding.py tests/test_artifact_grounding_server.py tests/test_artifact_grounding_compatibility.py tests/test_required_kinds_evidence.py tests/test_media_assets.py --basetemp .pytest-artifact-grounding-migration --maxfail=1
```

Result: **56 passed, 2 skipped**.

Ownership coverage proves:

- the packaged adapter owns the implementation and private validation seams;
- the root import resolves to the same module object;
- server and asset generation bind the packaged module directly;
- requirement parsing, artifact validation, risk/grounding semantics, and
  formatting remain covered by the existing focused suites.

Additional gates run for this slice:

- `python scripts/check_architecture.py` — pass;
- `python -m compileall -q sonder_runtime/adapters/artifact_grounding.py artifact_grounding.py server.py assetgen.py` — pass;
- `python scripts/check_requirement_evidence.py` — pass;
- `python scripts/check_evidence_documents.py` — pass;
- `python scripts/generate_documentation_catalogs.py --check` — pass.

## Verification boundary

This is an `implemented_unverified` ownership slice. Focused behavior and
packaging evidence pass, while formal checklist promotion and full-system
deployment/receipt verification remain separate requirements.

