# WP1 PDF risk ownership consolidation evidence

Date: 2026-08-21

## Scope

Retired the duplicated root `pdf_risk.py` compatibility path while preserving
the packaged `sonder_runtime/adapters/pdf_risk.py` implementation and native
MCP behavior.

## Ownership decision

`sonder_runtime/adapters/pdf_risk.py` is the sole implementation. The root
`pdf_risk.py` has been deleted and added to the permanent
`RETIRED_ROOT_MODULES` ratchet. Production tests and `server.py` use the
packaged import directly; `artifact_risk` continues to use the same packaged
PDF dependency.

## Completeness and security checks

- The packaged implementation retains bounded scan, source, decode, stream,
  and deadline limits; guarded file opening; sensitive-path and reparse-point
  rejection; encryption/incomplete-result handling; and
  `execution: "none"`.
- Ownership tests assert root absence, packaged ownership, and security-limit
  invariants.
- Existing adversarial PDF tests remain regression coverage for active-content
  detection, bounded decoding, incomplete scans, and path safety.

## Verification

Command: `python -m pytest -q tests/test_pdf_risk.py tests/test_pdf_risk_compatibility.py tests/test_artifact_risk.py tests/test_artifact_risk_compatibility.py tests/test_artifact_risk_policy.py tests/test_artifact_risk_server.py tests/test_package_local_system.py --basetemp .pytest-pdf-retire`

Result: pass - 76 tests passed, 2 skipped.

The initial default pytest invocation was blocked by an unrelated Windows
permission error scanning the host-local pytest temporary directory;
the same requested tests passed with `--basetemp .pytest-tmp-pdf`.
