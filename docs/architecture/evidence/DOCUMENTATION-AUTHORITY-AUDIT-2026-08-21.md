# Documentation authority audit — 2026-08-21

## Scope

`scripts/check_documentation_authority.py` reported four stale generated artifacts. The authoritative generator is `scripts/generate_documentation_catalogs.py`; its `expected()` projection was used to regenerate only the four paths named by the authority check:

- `docs/architecture/generated/runtime-reference.json`
- `docs/architecture/generated/runtime-reference.md`
- `docs/architecture/generated/architecture-map.json`
- `docs/architecture/generated/architecture-map.md`

No source code, non-generated architecture documentation, focused-contract inventory, or requirement-status artifact was edited.

## Verification evidence

Commands were run from the repository root:

```text
python scripts/check_documentation_authority.py
```

Result after repair: exit code `0`, no output.

```text
python scripts/generate_documentation_catalogs.py --check
```

Result after repair: exit code `0`, no output.

```text
python scripts/check_requirement_evidence.py
```

Result: exit code `0`, no output.

```text
python scripts/check_evidence_documents.py
```

Result: exit code `0`, no output.
