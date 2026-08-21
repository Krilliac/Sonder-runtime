# Requirement evidence audit after API-003 subprocess and archive-limit work — 2026-08-21

Date: 2026-08-21  
Scope: API-003 subprocess/provider evidence and archive-limit packaging work.  
Disposition: evidence audit only; the master checklist and authoritative ledger
were not edited.

## Decision summary

No formal requirement can be promoted in this checkpoint. The master
specification still has **0/204** checked requirements, and the latest
authoritative evidence records remain **204 planned / 0 verified**.

The archive-limit work does not create a new formal promotion candidate. It
strengthens the existing SEC-004 contract evidence and the archive-create
typed boundary, but it does not replace requirement-specific end-to-end,
parser-resource, integration, or reviewed ledger evidence.

## Exact remaining gaps

### API-003 — MCP

Finding: **PARTIAL contract evidence; not formally proven.**

New evidence in `API-003-SUBPROCESS-PROVIDER-2026-08-21.md` and
`tests/test_api003_subprocess_provider.py` proves a real child-process stdio
exchange: MCP 2.0 negotiation, tool listing, a bounded tool call, oversized
argument rejection, and bounded termination of a hanging child.

The remaining gaps are:

- the subprocess test does not exercise a negotiated subscription notification
  delivered by the child provider;
- the test uses an inline test provider, not a separately packaged/deployed
  external provider boundary; and
- forced termination in the test is not evidence of production process-tree
  supervision, cleanup receipts, or restart/reconciliation behavior.

Therefore API-003 may be described as having subprocess-boundary evidence, but
its formal checkbox and ledger status must remain unchanged.

### SEC-004 — Package/archive safety

Finding: **PROVEN-CONTRACT remains the appropriate audit classification; no
formal promotion.**

`ArchiveCreateLimits` now centralizes defaults and hard ceilings for the
archive-create path, and the typed archive adapter/executor preserves those
limits. Existing bounded archive inspection and executor tests cover path,
link, entry, per-entry, total-byte, depth/result, and non-replacement behavior.

The remaining formal gaps are complete requirement-scope evidence across all
archive/package entry points, parser-resource and expansion-ratio coverage,
production integration/recovery evidence, and a requirement-specific ledger
record with real baseline/verification digests. No such record is currently
verified.

## Bounded checks

| Check | Result |
|---|---|
| `python scripts/check_requirement_evidence.py` | PASS |
| `python scripts/check_evidence_documents.py` | PASS |
| `python scripts/check_documentation_authority.py` | PASS |
| `python scripts/generate_documentation_catalogs.py --check` | PASS |
| `python scripts/check_architecture.py` | BLOCKED: aborts because tracked production source `pdf_risk.py` is missing/not a regular file |
| Focused requirement/API/archive pytest command | PASS — 50 passed |

Focused pytest command:

```text
python -m pytest -q --basetemp .pytest-audit-20260821 tests/test_api003_subprocess_provider.py tests/test_api003_legacy_declaration.py tests/test_mcp_stdio_transport.py tests/test_native_mcp.py tests/test_wp9_path_archive_safety.py tests/test_archive_create_boundary.py tests/test_archive_create_executor.py tests/test_archive_extract_executor.py tests/production/test_requirement_evidence.py tests/test_requirement_audit_next.py
```

The architecture-check failure was not converted into a pass or worked around;
no missing source was recreated in this checkpoint.

## Protected formal records

`docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md` and
`docs/architecture/evidence/requirements.jsonl` remain unchanged. This file
is the only artifact added by this audit.
