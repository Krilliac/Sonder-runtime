# Read-only workbench family through the typed tool gateway — 2026-09-02

## Bounded slice

The seven read-only workbench tools — `directory_tree`, `file_find`,
`file_read` (typed name `read_file`), `file_read_range`, `text_search`,
`script_search`, `program_search` — now reach the guarded filesystem and
workbench primitives only through the application graph's typed tool gateway
(`Application.tools`, composed in `sonder_runtime/bootstrap/app.py` from the
descriptors and policy in `sonder_runtime/bootstrap/typed_tools.py`):

- the native MCP surface routes the family through `application.tools`
  (`bootstrap/native_mcp.py`, `typed_result`) and keeps its envelope shape;
- the seven legacy `server.py` handlers forward through
  `server._typed_read_tool` and keep live reload, the `include_ignored`
  policy text, activity records, grounded-outcome feeding and their exact
  output format; a source-level test pins that none of them calls
  `file_ops`/`workbench` directly any more.

One pipeline serves both: schema validation, the resource policy (which
admits exactly this family and denies everything else by default), the
runtime's permission modes adapted as a second `PermissionEvaluator`
(`adapters/security/permission_evaluator.py`, one decider, decided for the
request's own source with the console exemption only where a person drives),
deadline and cancellation, the packaged guards
(`adapters/typed_tool_executor.py` over `ToolExecutorAdapter`), output
redaction, and one durable receipt.

Receipts gained a terminal-reason vocabulary (`completed`, `failed`,
`cancelled`, `deadline_exceeded`, `policy_denied`); the gateway publishes a
receipt on every early exit before re-raising, so refusals and timeouts are
visible in the durable audit. `policy_match` names what each evaluator
matched and `model` is explicitly empty (a tool call is not a model call).
The request scope carries `source` and `auth_level`, and the invoker's
operation context is derived from them instead of a hard-coded console/local
context. The durable audit record (`tool-audit-record-v2`) carries `source`,
`auth_level`, `terminal`, `execution_world`, `argument_digest`,
`result_digest`, `effects`, `policy_match` and redacted `evidence`, and the
repository rotates a full file aside (naming the chain it continues from)
instead of failing every later call; rotation can be refused, in which case
the original fail-closed bound holds.

The packaged executor's `file_read_range` now applies the secret/control-plane
read guard the legacy handler applied, so the native surface enforces it too;
`read_file` evidence reports truncation, and both take developer
authorization from the operation context.

## Verification

- `python -m pytest -q tests/test_typed_read_family.py` — 26 passed
  (surface parity with identical `result_digest` on both surfaces, one
  audit record per call with `source` and `terminal`, identical refusal
  outside the roots, the read guard on the native line-range path, the
  permission gate on both surfaces, early-exit receipts, rotation, the
  handler ratchet).
- `python -m pytest -q tests/test_seam002_typed_gateway.py
  tests/test_crosscutting_tool_gateway.py tests/test_tool_audit_repository.py
  tests/test_native_mcp.py tests/test_legacy_tool_executor.py
  tests/test_tool_contract_conformance.py` — pass.
- `python scripts/check_architecture.py`, `check_requirement_evidence.py`,
  `check_error_signals.py`, `check_doc_links.py`,
  `generate_documentation_catalogs.py --check` — pass.

## Scope guard

The mutating file family, one-shot approvals and effect fencing are the next
slice and are not part of this one. No interface other than the native MCP
surface and the seven legacy handlers changed; the guards, the containment
rules and the legacy output formats are unchanged. Pattern redaction now
applies to the family's output on both surfaces, which is the one observable
behaviour change: credential-shaped strings inside read tool output are
scrubbed before they are shown or audited.
