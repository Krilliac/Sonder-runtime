# Mutating file family, one-shot approvals and effect fences — 2026-09-02

Slice 2 of the typed-tool migration, on top of
`TOOL-READ-FAMILY-TYPED-GATEWAY-2026-09-02.md`.

## Bounded slice

### The mutating file family through the typed gateway

The nine mutating file tools — `file_write` (typed `write_file`),
`file_edit` (`edit_file`), `directory_create` (`make_directory`),
`file_copy`, `file_move`, `file_batch_write`, `json_patch`, `text_patch`,
`file_delete` — now reach the guarded primitives only through
`Application.tools`, from the native MCP surface directly and from the legacy
`server.py` handlers through `server._typed_tool` (the read family's forward,
generalised). The family is declared once
(`bootstrap/typed_tools.py`: `MUTATING_TOOLS`, `TYPED_TOOLS`, `POLICY_NAMES`,
`GUARD_KNOBS`) and pinned against the native route.

- The legacy handlers keep their output formats byte for byte, including the
  structured refusals: the packaged executor carries a `BatchWriteError`,
  `JsonPatchError` or `TextPatchError` report as the failure's output and
  the handler renders it exactly as before. A source-level test pins that no
  handler in either family calls `file_ops`, `workbench`, `json_patch_tool`
  or `text_patch_ops` directly.
- The native canonical descriptors `write_file`, `make_directory` and
  `read_file` gained bounded schemas; they were `{"type": "object"}`, which
  admitted any argument — `bypass` included — on the native surface.
- A failure message on a receipt is redacted like output (it is shown and
  audited the same way); four guard messages were reworded to put the path
  before "developer token", because `token: <path>` read as a credential to
  the redactor and the two surfaces then disagreed.

### One decision per call

`ToolScope.gate` says who decides: `gateway` (the native surface, or any
caller with no gate of its own) or `surface` (an in-process surface already
decided at its entry point: the console after its prompt, the legacy MCP and
HTTP gates, the agent gate, the control chain). The permission evaluator
records `permission:surface` for the latter and does not decide again; the
read family's slice had it deciding a second time, which for reads was
harmless and for a console-approved write would have refused the call the
operator just answered yes to. Internal Python calls stay deliberately
ungated, as `permission_modes` documents. The gateway prefers an evaluator's
`authorize_request(request)` so the native decision carries the arguments.

### One-shot approvals

The unattended refusal of an effect class gained a fourth route out, beside an
allow rule, a mode that allows the class and the console prompt:

- `permission_modes.call_digest(tool, arguments)` digests the tool name and
  the canonical JSON of the arguments with the credential knobs removed
  (`CREDENTIAL_ARGUMENTS`); `Decision.call_id` is its first 16 hex characters
  and travels on the refusal reason, the receipt detail (`call_id`) and the
  native error evidence. The digest is stable across the token, approval or
  host-injected knob a surface adds, so the approved call and the retried
  call match.
- `adapters/security/approval_ledger.py` (`approvals.db` under the Sonder
  home, `SONDER_APPROVALS_DB`) records every unattended effect-class refusal
  that carried arguments as a pending call (digest, tool, surface, count, a
  bounded content-free preview passed through the platform redactor) and
  holds issued approvals (nonce, tool, digest, approver, surface, expiry,
  consumption). Consumption is one conditional update in an immediate
  transaction: two identical calls racing for one approval cannot both run.
- `decide()` consults the ledger at step 5c only: when it would otherwise
  refuse an effect class unattended, it spends an open approval for exactly
  this call (`source="approval"`) or notes the call as pending and names
  `/approve <call id>` among the remedies. A preflight (`record=False`)
  neither spends nor notes. `plan`, an explicit `deny`, the unclassified
  grade and the durable-authority class are untouched.
- The surfaces pass their arguments: the legacy MCP gate
  (`reloadable_mcp._refuse_if_gated`), the agent gate
  (`server._agent_permission_gate_error`), the control chain's `/<tool>`
  fall-through, the served `/<tool>` path and the native gateway. No call
  carries a nonce: the approval is matched by digest, so an agent's unchanged
  retry after the operator approved runs without the model ever seeing a
  secret, and the 206 legacy tools needed no schema change. This is a
  deliberate departure from the review's §10 sketch (nonce in the call).
- `permission_approve` issues (by call id from `/approvals`, or by tool and
  `arguments_json` before the call is made) and revokes. It is graded
  `dangerous`, is in `DURABLE_AUTHORITY_TOOLS` (so no unattended caller can
  approve its own next call), is an agent system-operator tool, and is bound
  to the admin role on the served surface. Authority: the console operator
  who answered the gate's prompt (`/approve`, carried by an in-process
  sentinel the way the project-root approval is), a developer token, or
  `SONDER_ALLOW_PERMISSION_EDITS=1`. `permission_approvals` (`/approvals`)
  is the read-only listing.

### The reach an approval carries, and the shared code's retirement

Decision 3 of the review is taken: the shared `SONDER_FILE_APPROVAL_CODE` is
retired. It was a static secret pasted into the model-visible `approval`
argument, reusable, unrecorded per use, and it switched containment off
entirely (`resolve_path` returned without a root check under `bypass`).
The variable is still scrubbed from child environments and logs, and a
process that has it set warns once that it no longer does anything.

In its place a spent one-shot approval carries exactly the reach the
operator approved. The digest binds the call's `extra_roots`; every surface
that decides a call installs `file_ops.reach_scope` around its gate and
handler (`server.approved_call_reach`: the legacy MCP `call_tool`, the
control chain's `/<tool>` path, the served `/<tool>` path, the observed
agent dispatch) and the native surface installs it around the gateway
call. The scope's provider is consulted at resolution time, so the roots
appear only after the gate has spent an approval for exactly this call
(`permission_modes.approval_spent_for`), are honoured through
`allowed_roots` with containment still checked against them, and vanish
when the call is over. A call approved with `extra_roots=/a` still cannot
write under `/b`. Reads are never approval-backed (the gate never refuses
them), so reach for a read outside the roots stays with the roots
configuration and the developer token.

Every agent path now drops a string `token` or `approval` from a model's
proposal before anything reads it (`server._agent_dispatch`); only the
host's in-process project-root sentinel survives, because it is not a
string. Autopilot and selfmod already refused or stripped them; the general
agent loop passed them through.

### Effect fences

`adapters/execution/effect_fence.py` carries a lease to the effects it
guards. `decide()` consults the current fence before any effect-class
decision on that thread (step 0, before rules and mode) and refuses with
`source="fence"` and a receipt once the fence reports itself lost; a check
that raises counts as lost. Reads are never fenced. The agent loop's refusal
text tells the model its authority is gone and not to retry. The typed
evaluator consults the same fence, so a native call made on a fenced thread
would be refused too. Three lease holders install one:

- the autopilot worker, `autopilot_fence(run_id, owner_id)` around each
  task (lost when the run's lease is gone or the run is cancelled);
- every fleet worker thread, `fleet_fence(agent_id, owner_id)` for as long
  as it is bound to its agent row (`master_orchestrator._bind_worker_agent`;
  lost when the owner heartbeat expires, the agent is reassigned or
  cancelled);
- the selfmod editing agent, `selfmod_fence(run_id, owner_id)` around the
  edit (`server._execute_selfmod_run`; lost when the run's lease expires,
  the run changes hands or leaves the editing phases).

### The evaluation lane

`tool_policy` scenarios gained `arguments` that reach the gate (a refusal
then names the call; `call_named` in the trajectory), a `fence` field
(`held` / `lost`) and an `expected_source` that pins which layer decided.
Seven shipped cases pin the new shapes (a named refusal on the agent and
MCP surfaces; a lost fence refusing an effect in `auto`, in the loop, and
over an allow rule; a held fence and a read left alone). A preflight never
spends an approval or notes a pending call, so the lane leaves the ledger
untouched.

## Verification

- `python -m pytest -q tests/test_typed_mutating_family.py` — 16 passed
  (both surfaces run one pipeline per mutation with one receipt each; legacy
  formats and structured refusals unchanged; containment identical; the
  native schema still refuses the guard knobs; a native call is decided
  exactly once and a legacy forward not again; an unattended native mutation
  is refused with a call id, approved once, refused again; `plan` refuses).
- `python -m pytest -q tests/test_permission_approvals.py` — 27 passed
  (digest and preview; the ledger's spend-once, expiry, revocation, prefix
  resolution, redaction and home; the decider's consumption, pending record,
  preflight, and the untouched `plan`/deny/durable cases; the legacy MCP,
  agent and control surfaces; the operator's tools and their gating).
- `python -m pytest -q tests/test_permission_approvals.py` also covers the
  reach: approved roots honoured once, containment still checked against
  them, no leak past the call, model credentials stripped, the retired code
  inert and warning once; `tests/test_typed_mutating_family.py` covers the
  native surface.
- `python -m pytest -q tests/test_effect_fence.py` — 14 passed (the fence
  itself, the decider, the agent gate, and the autopilot, fleet and selfmod
  holders).
- `python eval_harness.py run --suite tool_policy_gates --provider policy
  --check-baseline --no-record-history` — 33 pass / 0 fail; the baseline is
  re-pinned to the extended suite.
- `python -m pytest -q tests/test_typed_read_family.py
  tests/test_permission_modes.py tests/test_permission_durable_authority.py
  tests/test_permission_gate_dispatch.py tests/test_tool_contract_conformance.py
  tests/test_native_mcp.py tests/test_command_grading.py
  tests/test_permission_gate_coverage.py` — pass.
- `python scripts/check_architecture.py`, `check_requirement_evidence.py`,
  `check_error_signals.py`, `check_doc_links.py`,
  `generate_documentation_catalogs.py --check` — pass.

## Scope guard

The legacy `approval` parameter stays on the handlers' signatures: it still
carries the host's in-process project-root sentinel, and a protocol caller's
string in it is inert. `SONDER_ISOLATED_APPROVAL_CODE` and
`SONDER_ISOLATED_WRITE_APPROVAL_CODE` had the same shape and followed on
2026-09-03: `isolated_run` keeps its developer token and risk acknowledgement,
and a writable workspace needs a one-shot approval of exactly that call, spent
by the gate on an unattended refusal answered with `/approve`, or by the
handler itself when the mode let the call through
(`server._isolated_write_approved`, pinned in `tests/test_isolated_run_server.py`).
`ResourcePolicy.Decision.ALLOW_ONCE` remains
unused: the permission ledger is the one-shot mechanism. The compute job
worker is not fenced: its effect is the job it launches for a controller,
its claim is the job record itself, and cancellation already reaches the
process; a fence there would guard nothing the permission gate decides.
