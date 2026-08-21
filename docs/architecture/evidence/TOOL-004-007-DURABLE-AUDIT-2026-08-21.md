# TOOL-004/007 durable tool audit evidence — 2026-08-21

## Bounded slice

The packaged tool gateway now accepts an optional typed audit repository. It
passes the original request scope and the already-redacted `ToolReceipt` to a
durable adapter before publishing the ordinary receipt sink. The adapter
preserves `principal_id`, workspace roots, `session_id`, and `project_id`;
stores bounded JSONL records; and links records with a SHA-256 previous-record
digest. It never updates or deletes an existing record.

The adapter defensively redacts the serialized record as a second boundary.
Redaction failure, malformed redacted JSON, integrity failure, or capacity
exhaustion raises `ToolAuditError`. The gateway therefore publishes no receipt
when durable safe audit cannot be established. Raw tool output is never
written by this seam.

## Verification

- `python -m pytest -q tests/test_tool_audit_repository.py` — 4 passed.
- `python -m pytest -q tests/test_crosscutting_tool_gateway.py tests/test_tool_audit_repository.py` — focused gateway regression pass.
- `python -m compileall -q sonder_runtime tests` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_evidence_documents.py` — pass.
- `git diff --check` — pass; existing line-ending warnings only.

## Scope guard

Only the tool gateway/audit application and adapter seam, its focused tests,
and this evidence document are part of this slice. HTTP, MCP, jobs/session
lifecycle, execution spill, memory, training, data, agent fleet, evaluation,
update, operations, model, compaction, and selfmod implementations were not
changed.
