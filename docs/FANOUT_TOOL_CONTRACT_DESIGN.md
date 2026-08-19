# Tool-Contract Conformance Harness — Design

Status: design accepted, implementation in progress on branch
`agent/fable-tool-contract` (see the work log,
`FANOUT_TOOL_CONTRACT_WORKLOG.md`, for the checklist and verified results;
the "Holes" and "Enforcement" sections below describe the *intended* end
state and are marked done only in the work log once evidenced).
Date: 2026-08-12.

## Problem

Every user-reachable command surface in Sonder Runtime is gated, but the
*contract* each gate enforces lives in several hand-maintained declarations
that only agree by discipline:

- `sonder_serve.SYSTEM_OPERATION_TOOLS` binds tool names to role-gated
  operations at the HTTP boundary;
- `server._AGENT_SYSTEM_OPERATOR_TOOLS` refuses the same *kind* of tool on the
  agent path — but is a different, larger list;
- `sonder_serve._LOOP_GLOBAL_OPERATION_TYPES` re-states a third, smaller copy
  for `/loop` action payloads;
- `permission_modes.DURABLE_AUTHORITY_TOOLS` and `command_catalog._DANGEROUS`
  carry two more curated slices of the same judgement.

Nothing executable asserted that these stay in step. The recurring regression
class — fixed piecemeal at least three times in this repo's history
(`/runwindow`, `/training`, `/emotion`) — is a *new spelling* reaching work
whose *curated spelling* is gated. The measured live instances at the time of
writing (both closed by this change, see "Holes"):

- `/memory_privacy_repair` requires the developer role over HTTP, while the
  same tool ran for an ordinary served account when spelled as a `/loop`
  action (`{"type": "memory_privacy_repair"}`). Same for
  `memory_quality_repair`.
- `admin_accounts` is refused for agents as a system operation, but an
  ordinary served account reached the tool body over HTTP and was stopped only
  by the tool's own in-tool token check.

This document defines the executable contract, its enforcement points, and the
conformance harness that keeps every reachable spelling attached to the same
gate as its canonical name — with a deny-by-default outcome when the
declarations drift.

## Surfaces and their gates (inventory, traceable to code)

| # | Surface | Entry point | Name resolution | Typed input validation | Permission gate | Authority gate |
|---|---------|-------------|-----------------|------------------------|-----------------|----------------|
| S1 | Direct MCP (protocol client) | `reloadable_mcp.ReloadableMCPServer.call_tool` | exact registered name | MCPServer schema validation | `_refuse_if_gated` → `decide_for_caller(interactive=False, gate_control_exempt=True)` | none — trusted local operator surface |
| S2 | Console, named branches | `sonder_repl.main` (+ ~25 names forwarded to `server.control_command`) | `cmd ==` chains | per-branch parsing | `_named_command_gate` → `console_tools()` map → `_gate_tools` (strictest member; `interactive` iff a tty) | in-tool token checks where present |
| S3 | Console, catalogued `/<tool>` | `sonder_repl._run_catalogued` | `command_catalog.parse_invocation` | `parse_invocation` (unknown key → `ValueError`, typed coercion) | `_permission_gate` | in-tool |
| S4 | `control_command` fall-through | `server.control_command` | same catalog | same | `_control_tool_refusal` | in-tool |
| S5 | HTTP, curated slash chain | `sonder_serve._handle_slash` | `cmd ==` chains | per-branch parsing | `_http_slash_refusal` (map + read-only narrowing) | `_slash_system_operation` + `SYSTEM_OPERATION_TOOLS` via `_http_tool_refusal` |
| S6 | HTTP, catalogued `/<tool>` | `sonder_serve._dispatch_catalogued_tool` | `parse_invocation` | `parse_invocation` | `_http_tool_refusal` | `SYSTEM_OPERATION_TOOLS` role binding + task-boundary + loop-payload closure |
| S7 | HTTP REST routes | `do_GET`/`do_POST` (e.g. `/v1/permission-mode`) | fixed routes | JSON body checks | route-specific | `_system_operation_authority_error` at the route |
| S8 | Agent / workbench / autopilot | `server._agent_dispatch` | `_canonical_agent_tool_name` (`_AGENT_TOOL_ALIASES`) | args must be a JSON object; per-tool handling | `_agent_permission_gate_error` (`interactive=False`) | `_AGENT_SYSTEM_OPERATOR_TOOLS` refusal, read-only policy, project scoping, web/location gates, hosted local-only policy |
| S9 | Loop / workflow actions | `server._loop_dispatch` (also replayed by `workflow_run`) | `_loop_action_tool` (action alias → canonical tool) | per-action | `_loop_permission_refusal` (`decide`, `interactive=False`) | HTTP-side: loop-payload closure in `sonder_serve` (this change); task-boundary refusals |

Trust boundaries, unchanged by this work:

- **Local-first.** Direct MCP (S1), the console (S2–S4), and `local-open`
  HTTP are the single-operator surface: role gates pass, permission modes and
  explicit deny rules still bind. Served accounts (`account`/`both` modes) are
  least-privilege: `user` < `developer` < `admin`, enforced at the HTTP
  boundary (`SYSTEM_OPERATION_ROLES`), never by prompt text.
- **Models are not operators.** `_AGENT_SYSTEM_OPERATOR_TOOLS` refuses system
  operations on the agent path unconditionally (even unsafe-lab). `loop` and
  `workflow_run` are not agent-dispatchable (`workflow_run`'s branch sits
  behind the system-operator refusal; `loop` has no branch), so loop actions
  enter only through operator surfaces.
- **Fail closed on ignorance.** An unknown or uncatalogued name grades
  `UNCLASSIFIED`, which every non-interactive gate refuses;
  `CatalogUnavailable` refuses rather than returning an empty map.

## The contract model

One derivation module, `tool_contract.py` (root, stdlib-only, `server`
imported lazily like `command_catalog` does), publishes a **classifier**, not
another hand-maintained list:

```
ToolContract(
    name,                  # canonical tool / stand-in name
    registered,            # is a live MCP registration
    risk,                  # permission_modes.risk_of(name)
    http_operation,        # binding, "" when none, or the UNBOUND sentinel
    http_role,             # EFFECTIVE role at the boundary (UNBOUND -> admin)
    agent_operator,        # in server._AGENT_SYSTEM_OPERATOR_TOOLS
    durable_authority,     # in permission_modes.DURABLE_AUTHORITY_TOOLS
)
```

Spelling closure (catalog aliases, agent aliases, loop action names) is
asserted directly against the catalog and the canonicalizers in the
conformance tests rather than materialized as fields, and the
sensitive-parameter vocabulary lives in `activity_tracker` where the masks
read it — one home per fact.

Everything is read from the authoritative sources at call time — the live MCP
registry, the catalog derivations (`console_tools`, `http_slash_tools`,
`catalog()`), the server policy sets, `permission_modes`' sets, and
`sonder_serve`'s maps. The module never registers, allows, denies, or runs a
tool; enforcement stays in the gates. `system_operation_for(tool)` is the one
function the HTTP gates consult (see Enforcement), so the classifier is
load-bearing exactly once and testable everywhere else.

The **privileged shape** is defined by the runtime's own declarations, not by
name heuristics: a tool is privileged-shaped iff it is in
`_AGENT_SYSTEM_OPERATOR_TOOLS` or `DURABLE_AUTHORITY_TOOLS`. A name-shape
grammar (`admin_*`, `permission_*`, …) is used **only** in the static
conformance test as a completeness tripwire (with a reasoned allowlist for
verified reads like `admin_whoami`), never at runtime — a runtime name
heuristic would refuse benign reads and train operators to route around the
gate.

## Conformance invariants (the executable contract)

- **P1 — privileged closure over HTTP.** Every privileged-shaped tool that is
  dispatchable over HTTP is refused for an ordinary served account at the
  boundary: by its `SYSTEM_OPERATION_TOOLS` role binding, by the
  durable-authority non-degrade in `decide()`, or — when neither is declared —
  by the deny-by-default rule (E1 below). Exercised against the real
  `_http_tool_refusal`, per tool.
- **P2 — spelling/alias closure.** Every reachable spelling (catalogued
  `/<tool>`, native slash alias, agent alias, loop action name) resolves to a
  canonical tool before grading, and the gate outcome for the spelling equals
  the outcome for the canonical name. Loop-action spellings of role-bound
  tools carry the same role requirement as their `/<tool>` spelling (E2).
- **P3 — deny-by-default on drift.** A tool the catalog cannot classify is
  refused on every non-interactive surface; a blind catalog refuses; a
  privileged-shaped tool with no HTTP binding is admin-only rather than open
  (E1); a loop action whose canonical tool is role-bound is role-checked even
  if no hand map names it (E2).
- **P4 — local-open stays usable.** `local-open` (and the owner api-key)
  passes every role boundary; permission modes and explicit deny rules still
  bind there. The closures in this change key on *served* authority and do not
  narrow the single-operator surface.
- **P5 — served-account least privilege.** `user` accounts reach no system
  operation through any spelling (curated slash, catalogued name, loop action,
  saved workflow); `developer` accounts cross only developer boundaries, never
  admin ones.
- **P6 — typed input before dispatch.** An unknown `key=value` on the
  catalogued surfaces raises before the handler runs; non-dict agent args are
  refused; the MCP path validates against the registered schema.
- **P7 — redaction of sensitive parameters.** Argument values whose names
  carry secret vocabulary never survive into the activity ledger (`_safe_args`
  masks them at store time; `_redact_text` sweeps free text). The ledger's
  key vocabulary must cover the same names as the free-text regex — the gap
  (`pwd`, `passwd`, `credential`, `authorization`, `access_key`, `apikey`)
  is closed by E3 and pinned by the harness.
- **P8 — the maps cannot rot silently.** Structural drift checks: HTTP
  system-operation bindings must be agent-refused too
  (`SYSTEM_OPERATION_TOOLS ⊆ _AGENT_SYSTEM_OPERATOR_TOOLS` for registered
  tools); every binding names a declared operation and role; the authority
  name-grammar tripwire fails when a new privileged-shaped registration is
  declared nowhere.

## Holes found and closed (evidence in the harness)

- **H1 — `admin_accounts` open at the HTTP boundary.** Agent-refused,
  catalogued `ask`, no role binding: an ordinary served account reached the
  tool body (in-tool token check was the only stop). Closed by E1
  (deny-by-default: unbound system-operator tools require admin over HTTP).
- **H2 — loop-action spellings of `memory_privacy_repair` /
  `memory_quality_repair`.** Their `/<tool>` spellings require the developer
  role; their loop-action spellings ran for ordinary accounts.
  (`self_heal_repair` was caught, but only by the account task boundary — a
  different contract.) Closed by E2 (derived loop-payload closure).
- **H3 — `_LOOP_GLOBAL_OPERATION_TYPES` was a drift-prone third copy.** It
  named 4 of the 7 loop-reachable system operations. Replaced by derivation
  (E2): loop action → `server._loop_action_tool` → the same
  `system_operation_for` classifier the catalogued path uses.
- **H4 — redaction key vocabulary narrower than the text regex.**
  `{"pwd": "hunter2"}` survived into the (in-memory, detail-gated) activity
  ledger verbatim. Closed by E3.

## Enforcement changes (smallest coherent set)

- **E1 — deny-by-default for unbound system operations at the HTTP boundary**
  (`sonder_serve._http_tool_refusal`): when an auth context is present and the
  tool is in `_AGENT_SYSTEM_OPERATOR_TOOLS` but has no
  `SYSTEM_OPERATION_TOOLS` binding, require administrator authority. Admin,
  owner api-key, and `local-open` callers are unaffected
  (`_admin_authorized` passes); registration/metadata drift now fails closed
  for shared accounts instead of open.
- **E2 — derived loop-payload closure** (`sonder_serve.
  _loop_global_operation_refusal`): resolve each action to its canonical tool
  via `server._loop_action_tool` and apply the same role binding (and E1
  rule) as the tool's own `/<tool>` spelling. The hand map is deleted, not
  extended.
- **E3 — align the activity ledger's sensitive-key vocabulary** with the
  free-text secret regex (`activity_tracker._safe_args` and `_safe_command`).
  Strictly widens redaction; never widens exposure.

Compatibility consequences, chosen deliberately (narrower host-enforced
policy, per the standing constraints):

- Ordinary served accounts lose loop-action access to
  `memory_privacy_repair`/`memory_quality_repair` (now developer, same as the
  direct spelling) — this is the closed bypass, not collateral.
- Served developer accounts in `both`/`account` modes lose loop-action access
  to `self_heal_repair` unless admin (`selfmod_deploy`), matching the direct
  spelling; they were already refused by the task boundary when
  account-scoped.
- Non-admin served accounts are refused `admin_accounts` at the boundary with
  a role message instead of reaching the tool's own "admin token required"
  error. No caller who previously *succeeded* loses anything.
- `local-open`, direct MCP, and the console keep their historical behavior on
  every one of these paths.

## Non-goals

- `tool_capabilities.py` descriptors stay shadow-phase; this work does not
  make them authoritative and does not add 180 descriptors.
- No narrowing of direct MCP or console trust; no gating of internal Python
  calls; no natural-language routing anywhere.
- No new hand-maintained alias lists: the two structural additions (the
  authority name-grammar tripwire's allowlist, with per-entry reasons and a
  dead-entry check) live in the test, following
  `test_permission_gate_coverage._DISPLAY_ONLY_BRANCHES`' pattern.
- Rate limiting, transport auth, and account storage are out of scope.

## Harness layout

- `tool_contract.py` — the classifier and `validate_contracts()` (structural
  drift errors as data, in the style of `tool_capabilities.validate_shadow`).
- `tests/test_tool_contract_conformance.py` — P1…P8 against the real gates:
  real `_http_tool_refusal` / `_handle_slash` / `_dispatch_catalogued_tool`
  with synthetic auth contexts, real `_agent_dispatch` refusals, real
  `decide()` with a neutralized rule lookup, loop payloads through the real
  `_loop_global_operation_refusal`, and redaction through the real
  `activity_tracker`. Bypass-shaped regressions are pinned by mutation-style
  tests (a spelling with a missing binding must fail the suite).
- Existing suites remain the base: this harness *adds* the cross-surface
  parity layer; it does not restate `test_permission_gate_*`'s per-surface
  behavior.
