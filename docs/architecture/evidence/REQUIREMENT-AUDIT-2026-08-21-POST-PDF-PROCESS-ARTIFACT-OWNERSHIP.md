# Formal evidence audit after PDF/process/artifact ownership work — 2026-08-21

Date: 2026-08-21  
Scope: PDF-risk implementation ownership, process-tree cleanup ownership, and
artifact/archive compatibility boundaries.  
Disposition: audit evidence only. The master checklist, authoritative ledger,
and generated requirement-status projections were not edited.

## Formal state

The authoritative state remains **204/204 latest ledger records `planned`**,
**0/204 `verified`**, and **0/204 master-spec checkboxes checked**. No formal
requirement is proven for checklist or ledger purposes by this refresh.

`PROVEN-CONTRACT` below means that the named contract has direct implementation,
focused evidence, and the tests listed here. It does not mean the formal
requirement is verified when the requirement also requires complete integration,
deployment, persistence, platform, recovery, parser-resource, or end-to-end
evidence.

## Exact contract-proven requirements

The following 162 requirements remain `PROVEN-CONTRACT` in the scoped audit:

```text
SESSION-001 SESSION-002 SESSION-003 SESSION-004 SESSION-005 SESSION-006 SESSION-007 SESSION-008 SESSION-009 SESSION-010
LOOP-001 LOOP-002 LOOP-003 LOOP-004 LOOP-005 LOOP-006 LOOP-007 LOOP-008
SEAM-001 SEAM-002 SEAM-003 SEAM-004 SEAM-005 SEAM-006 SEAM-007 SEAM-008 SEAM-009 SEAM-010 SEAM-011 SEAM-012 SEAM-013 SEAM-014 SEAM-015 SEAM-016
CTX-001 CTX-002 CTX-003 CTX-004 CTX-005 CTX-006 CTX-007 CTX-008 CTX-009 CTX-010
REPO-001 REPO-002 REPO-003 REPO-004 REPO-005 REPO-006 REPO-007
SKILL-001 SKILL-002 SKILL-003 SKILL-004 SKILL-005 SKILL-006
AGENT-001 AGENT-002 AGENT-003 AGENT-004 AGENT-005 AGENT-006 AGENT-007 AGENT-008 AGENT-009 AGENT-010
JOB-001 JOB-002 JOB-003 JOB-004 JOB-005
TOOL-001 TOOL-002 TOOL-003 TOOL-004 TOOL-005 TOOL-006 TOOL-007
EXEC-001 EXEC-002 EXEC-003 EXEC-004 EXEC-005 EXEC-006
MEM-001 MEM-002 MEM-003 MEM-004 MEM-005 MEM-006 MEM-007 MEM-008
EVAL-001 EVAL-002 EVAL-003 EVAL-004 EVAL-005 EVAL-006 EVAL-007 EVAL-008 EVAL-009
MODEL-001 MODEL-002 MODEL-003 MODEL-004 MODEL-005 MODEL-006 MODEL-007 MODEL-008 MODEL-009 MODEL-010
API-001 API-002 API-004 API-005 API-006 API-007 API-008
DATA-001 DATA-002 DATA-003 DATA-004 DATA-005 DATA-006 DATA-007
OPS-001 OPS-002 OPS-003 OPS-004 OPS-005 OPS-006
SEC-001 SEC-002 SEC-003 SEC-004 SEC-005 SEC-006 SEC-007 SEC-008 SEC-009
TRAIN-001 TRAIN-002 TRAIN-003 TRAIN-004 TRAIN-005 TRAIN-006 TRAIN-007 TRAIN-008 TRAIN-009
UPDATE-001 UPDATE-002 UPDATE-003 UPDATE-004 UPDATE-005
DOC-001 DOC-002 DOC-003 DOC-004 DOC-005 DOC-006 DOC-007
```

Ownership refresh evidence for the directly affected rows:

| Requirement | Exact current evidence | Finding and remaining proof |
|---|---|---|
| SEC-004 | `sonder_runtime/adapters/pdf_risk.py`, `pdf_risk.py`, `archive_create.py`, `sonder_runtime/adapters/archive_create.py`, `sonder_runtime/application/ports/archive_create_limits.py`; `tests/test_pdf_risk.py`, `tests/test_pdf_risk_compatibility.py`, `tests/test_archive_create_boundary.py`, `tests/test_archive_create_executor.py`, `tests/test_archive_extract_executor.py` | **PROVEN-CONTRACT.** PDF ownership, bounded PDF inspection, archive limits, path/link/entry/byte/depth/result bounds, and non-replacement behavior are covered. Full requirement proof still lacks complete package-entry-point coverage, parser-resource/expansion-ratio evidence, and formal verified ledger evidence. |
| JOB-004 | `sonder_runtime/adapters/process_tree_supervisor.py`, `sonder_runtime/application/ports/process_probe.py`; `tests/test_process_tree_supervisor.py`, `tests/test_process_probe_ownership.py` | **PROVEN-CONTRACT.** Typed full-tree cleanup and platform-specific fail-closed behavior are covered. Every execution-provider integration and production recovery proof remain unproven. |
| LOOP-006 | `sonder_runtime/adapters/process_tree_supervisor.py`, `sonder_runtime/application/loop/`; `tests/test_process_tree_supervisor.py` | **PROVEN-CONTRACT.** The cleanup contract is covered; complete propagation through every listed stream/tool/subprocess/terminal/subagent/job/training/update/selfmod path remains unproven. |
| DATA-007 | `sonder_runtime/adapters/artifact_fetch.py`, `artifact_fetch.py`, `sonder_runtime/application/artifacts/immutable_manifest.py`; `tests/test_artifact_fetch.py`, `tests/test_artifact_fetch_compatibility.py` | **PROVEN-CONTRACT.** Canonical artifact-fetch ownership and compatibility identity are covered. Full hash/manifest binding across every training, selfmod, update, session, and deliverable surface remains unproven. |
| TOOL-006 | `sonder_runtime/adapters/command_surface.py`, generated catalogs, `scripts/check_documentation_authority.py`, `scripts/generate_documentation_catalogs.py` | **PROVEN-CONTRACT.** Catalog/documentation authority checks passed. Complete generated-surface acceptance across all external clients remains a formal integration obligation. |

## Exact unproven requirements

These 42 requirements are **not proven by this refresh**. The first 41 were
outside the scoped contract audit; API-003 remains explicitly partial.

```text
CORE-001 CORE-002 CORE-003 CORE-004 CORE-005 CORE-006 CORE-007 CORE-008 CORE-009 CORE-010
ARCH-001 ARCH-002 ARCH-003 ARCH-004 ARCH-005 ARCH-006 ARCH-007 ARCH-008 ARCH-009 ARCH-010 ARCH-011 ARCH-012 ARCH-013
COMPACT-001 COMPACT-002 COMPACT-003 COMPACT-004 COMPACT-005
EXT-001 EXT-002 EXT-003 EXT-004 EXT-005 EXT-006 EXT-007
SELFMOD-001 SELFMOD-002 SELFMOD-003 SELFMOD-004 SELFMOD-005 SELFMOD-006
API-003
```

API-003 has negotiated MCP/native/legacy declaration and bounded subprocess
contract evidence, but a separately deployed provider boundary and production
process-tree supervision/recovery evidence remain unproven. No checkbox or
ledger promotion is justified for it or for any other row.

## Exact commands and results

All commands were run from the repository root. The pytest run used an
isolated workspace base directory to avoid the known shared-temp permission
failure.

| Check | Exact command | Result |
|---|---|---|
| Requirement/ledger | `python scripts/check_requirement_evidence.py` | PASS, exit 0 |
| Evidence documents | `python scripts/check_evidence_documents.py` | PASS, exit 0 |
| Documentation authority | `python scripts/check_documentation_authority.py` | PASS, exit 0 |
| Documentation catalogs | `python scripts/generate_documentation_catalogs.py --check` | PASS, exit 0 |
| Architecture | `python scripts/check_architecture.py` | PASS, exit 0 |
| Focused ownership/evidence tests | `python -m pytest -q --basetemp .pytest-audit-refresh-20260821 tests/test_pdf_risk.py tests/test_pdf_risk_compatibility.py tests/test_process_tree_supervisor.py tests/test_process_probe_ownership.py tests/test_artifact_fetch.py tests/test_artifact_fetch_compatibility.py tests/test_archive_create_boundary.py tests/test_archive_create_executor.py tests/test_archive_extract_executor.py tests/test_requirement_audit_next.py tests/production/test_requirement_evidence.py` | PASS, **98 passed** |

## Protected records

The master checklist and `docs/architecture/evidence/requirements.jsonl` were
not edited. This document is the sole artifact added by this refresh.
