# Requirement evidence audit — next formalization pass

Date: 2026-08-20  
Audited branch: `agent/wp1-execution-status`  
Audit baseline: current worktree at commit `9848ad4`  
Scope: SESSION/LOOP/SEAM/CTX/REPO/SKILL/AGENT/JOB/TOOL/EXEC/MEM/EVAL/MODEL/API/DATA/OPS/SEC/TRAIN/UPDATE/DOC

## Decision summary

This is an evidence audit, not a formal checklist update. The master
specification and its formal checkboxes were not edited.

- Requested scope: **163 IDs** across 19 families.
- Formal master-spec checkboxes: **0/250 checked**.
- Formal evidence ledger: **204 latest records; all 204 are `planned`**;
  none are `verified`.
- Safe formal checkbox candidates in this audit: **none**.
- Direct contract evidence exists for many IDs, but a contract slice is not
  automatically an end-to-end requirement proof. Most evidence documents state
  that integration, durable adapters, transport wiring, or production migration
  remains outside the slice.

The safe rule for the next change is: promote an ID only when its requirement
text, production path, focused tests, selected regression tests, architecture
gate, evidence-ledger record, and any required persistence/platform behavior
are all demonstrated in the same reviewed change. This audit deliberately does
not create ledger records or checkboxes.

## Evidence classes

| Finding | Meaning |
|---|---|
| `PROVEN-CONTRACT` | A current implementation/evidence document and focused test directly demonstrate the named contract. This is not formal completion when the requirement also demands integration, persistence, platform behavior, or end-to-end use. |
| `PARTIAL` | Some relevant implementation or evidence exists, but the requirement has an explicit non-goal, an unproved integration boundary, or only a narrower foundation. |
| `MISSING` | No current scoped evidence was found that is sufficient even for the named requirement slice. |

## Requirement-by-requirement findings

### SESSION

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SESSION-001 | PROVEN-CONTRACT | `WP2-SESSION-001.md`; `sonder_runtime/domain/common/ids.py`; `tests/test_domain_ids.py`. |
| SESSION-002 | PARTIAL | `WP2-SESSION-002-008.md`; repository foundation exists, but durable append-only persistence and integrated callers are not proven. |
| SESSION-003 | PROVEN-CONTRACT | `WP2-SESSION-003.md`; `sonder_runtime/domain/session/events.py`; `tests/test_session_event_schema.py`. |
| SESSION-004 | PARTIAL | `WP2-SESSION-004-005.md`; replay envelope foundation exists, but complete model-visible capture is not proven. |
| SESSION-005 | PARTIAL | `WP2-SESSION-004-005.md`; `tests/test_session_replay.py`; full request/transcript/UI/tool reconstruction is not proven. |
| SESSION-006 | PROVEN-CONTRACT | `WP2-SESSION-006.md`; `sonder_runtime/application/session/fork.py`; `tests/test_session_fork.py`. |
| SESSION-007 | PROVEN-CONTRACT | `WP2-SESSION-007.md`; `sonder_runtime/application/session/repair.py`; `tests/test_session_repair.py`. |
| SESSION-008 | MISSING | No complete bounded search, transcript export, and event export path with end-to-end evidence was found. |
| SESSION-009 | PROVEN-CONTRACT | `WP2-SESSION-009.md`; `sonder_runtime/application/session/checkpoints.py`; `tests/test_session_checkpoints.py`. Durable adapter integration remains unproved. |
| SESSION-010 | PROVEN-CONTRACT | `WP2-SESSION-010.md`; `tests/test_session_privacy.py`. End-to-end retention execution remains unproved. |

### LOOP

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| LOOP-001 | PROVEN-CONTRACT | `WP2-LOOP-001-003.md`; `sonder_runtime/application/loop/contract.py`; `tests/test_loop_contract.py`. |
| LOOP-002 | PROVEN-CONTRACT | `WP2-LOOP-001-003.md`; loop step contract and focused tests. |
| LOOP-003 | PROVEN-CONTRACT | `WP2-LOOP-001-003.md`; typed interception events and focused tests. |
| LOOP-004 | PROVEN-CONTRACT | `WP2-LOOP-004.md`; `sonder_runtime/application/loop/events.py`; `tests/test_loop_event_classification.py`. |
| LOOP-005 | PROVEN-CONTRACT | `WP2-LOOP-005.md`; `tests/test_loop_steering.py`. |
| LOOP-006 | PARTIAL | `WP2-LOOP-006.md`; cancellation tree is tested, but stream/tool/provider cleanup conformance is not complete. |
| LOOP-007 | PARTIAL | `WP2-LOOP-007-008.md`; policy and tests exist, but adapter retry execution and evidence retention are explicitly outside the slice. |
| LOOP-008 | PARTIAL | `WP2-LOOP-007-008.md`; key policy exists, but persistent idempotency/reconciliation integration is not proven. |

### SEAM

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SEAM-001 | PROVEN-CONTRACT | `WP3-SEAM-001.md`; `sonder_runtime/application/ports/model_gateway_contract.py`; `tests/test_model_gateway_contract_wp3.py`. |
| SEAM-002 | PROVEN-CONTRACT | `WP3-SEAM-002.md`; tool registry/executor ports and `tests/test_wp3_seam002_tool_contract.py`. Adapter migration remains outside the slice. |
| SEAM-003 | PROVEN-CONTRACT | `WP3-SEAM-003.md`; filesystem port and `tests/test_filesystem_port_wp3.py`. Caller/provider migration remains unproved. |
| SEAM-004 | PROVEN-CONTRACT | `WP3-SEAM-004.md`; execution-world port and `tests/test_execution_world_port.py`. Concrete adapter unification remains unproved. |
| SEAM-005 | PROVEN-CONTRACT | `WP3-SEAM-005.md`; sandbox port and `tests/test_sandbox_provider_port.py`. A real sandbox security claim is not made. |
| SEAM-006 | MISSING | No complete integrated SessionRepository/SessionQueryEngine seam with bounded query/export evidence was found. |
| SEAM-007 | PROVEN-CONTRACT | `WP3-SEAM-007.md`; compaction port and `tests/test_wp3_seam007_compaction.py`. |
| SEAM-008 | PROVEN-CONTRACT | `WP3-SEAM-008.md`; skill registry contract and `tests/test_skill_registry.py`. |
| SEAM-009 | PROVEN-CONTRACT | `WP3-SEAM-009.md`; provider-neutral child contract exists, but a production provider is not proven. |
| SEAM-010 | PROVEN-CONTRACT | `WP3-SEAM-010.md`; job/workflow ports and `tests/test_wp3_seam010_jobs.py`. Existing stores/transports remain outside the slice. |
| SEAM-011 | PROVEN-CONTRACT | `WP3-SEAM-011.md`; web/credential ports and `tests/test_wp3_seam011_web.py`. |
| SEAM-012 | PROVEN-CONTRACT | `WP3-SEAM-012-013.md`; artifact port and focused contract tests. |
| SEAM-013 | PROVEN-CONTRACT | `WP3-SEAM-012-013.md`; telemetry port and focused contract tests. |
| SEAM-014 | PARTIAL | `WP3-SEAM-014.md`; specialized lifecycle ports are tested, but providers are not wired. |
| SEAM-015 | PROVEN-CONTRACT | `WP3-SEAM-015.md`, `CROSSCUT-SEAM-015-016.md`; `tests/test_provider_override_policy.py`, `tests/test_crosscutting_provider_lifecycle.py`. |
| SEAM-016 | PROVEN-CONTRACT | `WP3-SEAM-016.md`, `CROSSCUT-SEAM-015-016.md`; `tests/test_provider_lifecycle.py`, `tests/test_crosscutting_provider_lifecycle.py`. |

### CTX

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| CTX-001 | PROVEN-CONTRACT | `WP4-CTX-001-002.md`; `sonder_runtime/application/context_planner.py`; focused planner tests. |
| CTX-002 | PROVEN-CONTRACT | `WP4-CTX-001-002.md`; explicit section budgets and planner tests. |
| CTX-003 | PROVEN-CONTRACT | `WP4-CTX-003-005.md`; priority/eviction policy and `tests/test_wp4_ctx003_005.py`. |
| CTX-004 | PROVEN-CONTRACT | `WP4-CTX-004-006-009-010.md`; manifest deduplication and `tests/test_wp4_ctx004_006_009_010.py`. |
| CTX-005 | PROVEN-CONTRACT | `WP4-CTX-003-005.md`; explainability fields and focused tests. |
| CTX-006 | PROVEN-CONTRACT | `WP4-CTX-004-006-009-010.md`; last-good manifest/snapshot behavior and focused tests. |
| CTX-007 | PROVEN-CONTRACT | `WP4-CTX-007.md`; overflow recovery and `tests/test_context_overflow_recovery.py`. |
| CTX-008 | PROVEN-CONTRACT | `WP4-CTX-008.md`; measured hardware sizing and `tests/test_wp4_ctx008.py`. |
| CTX-009 | PROVEN-CONTRACT | `WP4-CTX-004-006-009-010.md`; prefix identity/caching manifest contract and focused tests. |
| CTX-010 | PROVEN-CONTRACT | `WP4-CTX-004-006-009-010.md`; exact request manifest replay contract and focused tests. |

### REPO

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| REPO-001 | PROVEN-CONTRACT | `WP4-REPO-001-003-005.md`; incremental index/map implementation and repository intelligence tests. |
| REPO-002 | PROVEN-CONTRACT | `WP4-REPO-002.md`; `sonder_runtime/domain/repository_languages.py`; `tests/test_repository_languages.py`. |
| REPO-003 | PROVEN-CONTRACT | `WP4-REPO-001-003-005.md`; ranked map contract and focused tests. |
| REPO-004 | PARTIAL | `WP4-REPO-004-006-007.md`; navigation seam exists, but no live LSP integration is proven. |
| REPO-005 | PROVEN-CONTRACT | `WP4-REPO-001-003-005.md`; digest-bound evidence contract and tests. |
| REPO-006 | PROVEN-CONTRACT | `WP4-REPO-004-006-007.md`; progressive symbol expansion contract and `tests/test_repository_navigation.py`. |
| REPO-007 | PARTIAL | `WP4-REPO-004-006-007.md`; cross-root shape is described, but multi-repository implementation/evidence is incomplete. |

### SKILL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SKILL-001 | PROVEN-CONTRACT | `WP4-SKILL-001-002.md`; registry discovery and `tests/test_wp4_skill_registry.py`. |
| SKILL-002 | PROVEN-CONTRACT | `WP4-SKILL-001-002.md`; progressive metadata/content disclosure and focused tests. |
| SKILL-003 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; refresh/digest implementation and `tests/test_skill_refresh_plugin_manifest.py`. |
| SKILL-004 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; source/trust/version policy fields and focused tests. |
| SKILL-005 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; malformed/repeated-failure quarantine contract and focused tests. |
| SKILL-006 | PARTIAL | WP6 promotion evidence and `REMAINING-MEM-001-008.md` support procedural promotion, but durable publication and operational promotion are not proven. |

### AGENT

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| AGENT-001 | PROVEN-CONTRACT | `WP5-AGENT-001.md`; registry adapter and `tests/test_wp5_fleet_autopilot.py`. Full unified service integration remains open. |
| AGENT-002 | PROVEN-CONTRACT | `WP5-AGENT-002.md`; Workbench/review adapters and `tests/test_wp5_workbench_review.py`. |
| AGENT-003 | PROVEN-CONTRACT | `WP5-AGENT-003.md`; roles/presets/budgets and `tests/test_wp5_roles_presets_budgets.py`. |
| AGENT-004 | MISSING | No complete built-in general/code/plan/reviewer/researcher preset catalog with end-to-end use was found. |
| AGENT-005 | MISSING | Durable parent/child lineage persistence and operator exposure are not evidenced. |
| AGENT-006 | PARTIAL | `WP5-SUBAGENT-001.md`; continuable child service is tested, but the repository is an in-memory reference adapter, not a production durability proof. |
| AGENT-007 | PROVEN-CONTRACT | `WP5-AGENT-003.md`, `WP5-SUBAGENT-001.md`; role/depth/count/concurrency budget foundations are tested, but full enforcement integration is open. |
| AGENT-008 | MISSING | Explicit isolated workspace assignment and enforcement are not evidenced. |
| AGENT-009 | MISSING | Structured delegation/result integration is not evidenced. |
| AGENT-010 | MISSING | Explorer/architect/editor/reviewer workflow integration is not evidenced. |

### JOB

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| JOB-001 | PROVEN-CONTRACT | `WP5-JOB-001.md`; typed dependency-ordered generic jobs and `tests/test_wp5_generic_jobs.py`. |
| JOB-002 | PARTIAL | `REMAINING-EXEC-001-006.md`; lifecycle/output control exists, but one durable registry across shell/terminal/workflow is not proven. |
| JOB-003 | PARTIAL | `WP5-WORKFLOW-001.md`, `REMAINING-EXEC-001-006.md`; recovery contracts are tested, but durable production reconciliation is open. |
| JOB-004 | PARTIAL | `REMAINING-EXEC-001-006.md`; termination intent/lifecycle is covered, but full process-tree containment is not proven. |
| JOB-005 | PROVEN-CONTRACT | `REMAINING-EXEC-001-006.md`; bounded pages, cursors, watermarks, truncation, and spill references are tested. Durable integration remains open. |

### TOOL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| TOOL-001 | PROVEN-CONTRACT | `CROSSCUT-TOOL-001-005.md`; `tests/test_crosscutting_tool_gateway.py` verifies one gateway pipeline. |
| TOOL-002 | PROVEN-CONTRACT | Same gateway evidence verifies schema, scope, permission, approval, deadline, cancellation, redaction, and receipt order. |
| TOOL-003 | PARTIAL | Typed scope/policy exists, but complete resource-aware path/host/resource matching across all adapters is not proven. |
| TOOL-004 | PROVEN-CONTRACT | Gateway tests cover allow/deny/approval modes; session/project persistence is not proven. |
| TOOL-005 | PARTIAL | Startup authority boundary is represented, but independent runtime authority wiring is not proven. |
| TOOL-006 | MISSING | Generated MCP/OpenAI/CLI/client catalogs are not evidenced. |
| TOOL-007 | PROVEN-CONTRACT | Gateway receipt/redaction tests prove the contract; durable audit storage is not proven. |

### EXEC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| EXEC-001 | PROVEN-CONTRACT | `REMAINING-EXEC-001-006.md`; shared world bindings and `tests/test_remaining_execution_world.py`. |
| EXEC-002 | PARTIAL | The world-kind contract distinguishes container/read-only worlds, but a default guarded container implementation is not evidenced. |
| EXEC-003 | PROVEN-CONTRACT | Remaining execution tests cover terminal/job lifecycle, reconnection, bounded reads, and watermarks; a concrete persistent terminal adapter remains open. |
| EXEC-004 | PROVEN-CONTRACT | Remaining execution evidence covers digest-bound spill references and bounded output. Durable spill store wiring remains open. |
| EXEC-005 | PARTIAL | Remote world identity/boundary is represented, but a configured remote worker implementation is not evidenced. |
| EXEC-006 | PROVEN-CONTRACT | Isolation truth labels and fail-closed mismatch behavior are directly tested. |

### MEM

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| MEM-001 | PROVEN-CONTRACT | `REMAINING-MEM-001-008.md`, `WP6-MEMORY-001.md`; typed memory classes and policy matrix are tested. |
| MEM-002 | PROVEN-CONTRACT | Memory admission/retrieval policy, privacy, provenance, confidence, and promotion rules are tested. |
| MEM-003 | PROVEN-CONTRACT | `WP6-MEMORY-001.md`; weighted evidence and procedural learning records are tested. |
| MEM-004 | PROVEN-CONTRACT | Temporal truth, supersedes, contradiction, decay, and revalidation are tested. |
| MEM-005 | PROVEN-CONTRACT | Retrieval explanations with score components, provenance, freshness, and exclusions are tested. |
| MEM-006 | PROVEN-CONTRACT | Embedding identity/binding validation is tested and documented in the remaining memory slice. |
| MEM-007 | PROVEN-CONTRACT | `WP6-MEMORY-002.md`; labeled retrieval metrics and bounded evaluation are tested. |
| MEM-008 | PARTIAL | Procedural promotion linkage is tested, but durable publication and active skill integration remain open. |

### EVAL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| EVAL-001 | PROVEN-CONTRACT | `WP6-EVAL-001.md`, `REMAINING-EVAL-001-009.md`; first-class suite/result/proposal records are tested. |
| EVAL-002 | PROVEN-CONTRACT | Evaluation suite identity and bounded result dimensions are tested; complete repository/tool/memory corpus coverage is not proven. |
| EVAL-003 | PROVEN-CONTRACT | Metrics and bounded measurements are tested in trajectory/retrieval/promotion slices. |
| EVAL-004 | PROVEN-CONTRACT | Result identity binds model/route/prompt/replay/provenance dimensions in lifecycle tests. |
| EVAL-005 | PROVEN-CONTRACT | `WP6-EVAL-001.md`; deterministic trajectory replay and canonical digests are tested. |
| EVAL-006 | PROVEN-CONTRACT | Replay divergence index/reporting is tested. |
| EVAL-007 | PROVEN-CONTRACT | `WP6-PROMOTION-001.md`; metric, holdout, provenance, regression, and rollback gates are tested. |
| EVAL-008 | PROVEN-CONTRACT | Shadow/canary health gating is tested in `tests/test_remaining_evaluation_lifecycle.py`. |
| EVAL-009 | PROVEN-CONTRACT | Proposal state transitions, attended promotion, immutable evidence, and rollback are tested. Durable lifecycle integration remains open. |

### MODEL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| MODEL-001 | PROVEN-CONTRACT | `WP7-MODEL-001.md`, `WP1-MODEL-ROOT-REMOVAL.md`; gateway contracts, injected adapters, and focused tests. Full caller migration remains open. |
| MODEL-002 | PROVEN-CONTRACT | `WP7-MODEL-002.md`; pure capability route planner and `tests/test_wp7_capability_routing.py`. |
| MODEL-003 | MISSING | Logical fast/code/general/reasoning/vision role catalog is not sufficiently evidenced. |
| MODEL-004 | PROVEN-CONTRACT | `WP7-MODEL-001.md`; measured calibration profiles and `tests/test_wp7_calibration.py`. |
| MODEL-005 | PROVEN-CONTRACT | `WP7-MODEL-002.md`; capability profiles and routing tests. |
| MODEL-006 | MISSING | MoE total-versus-active parameter residency correctness is not evidenced. |
| MODEL-007 | MISSING | Controlled escalation after uncertainty/verifier failure is not evidenced. |
| MODEL-008 | MISSING | Independent planner/editor/reviewer role budgets are not evidenced. |
| MODEL-009 | PARTIAL | Provider lifecycle health contracts exist, but configured/available/ready/degraded integration is not proven. |
| MODEL-010 | MISSING | NPU boundary behavior is not evidenced. |

### API

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| API-001 | PROVEN-CONTRACT | `WP8-API-001-002.md`; typed event vocabulary and `tests/test_wp8_protocol_streams.py`. |
| API-002 | PROVEN-CONTRACT | Same evidence covers snapshots, monotonic sequence numbers, resume watermarks, and bounded batches. |
| API-003 | PROVEN-CONTRACT | `WP8-API-003.md`; MCP negotiation/legacy declaration and focused compatibility tests. Network/provider integration is intentionally absent. |
| API-004 | PROVEN-CONTRACT | `WP8-API-004.md`; OpenAI compatibility mapping and focused tests. |
| API-005 | PROVEN-CONTRACT | `WP8-API-005.md`; bounded editor/agent envelopes and safe rule exchange tests. |
| API-006 | PROVEN-CONTRACT | `WP8-API-006.md`; operator control-plane snapshot and focused tests. |
| API-007 | MISSING | Flutter reconnect/resume and mobile parity are not evidenced. |
| API-008 | MISSING | Generated runtime-derived client/SDK schema catalogs are not evidenced. |

### DATA

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| DATA-001 | PARTIAL | `CROSSCUT-DATA-001-004.md`; persistence boundary exists, but per-domain SQLite ownership is not proven. |
| DATA-002 | PROVEN-CONTRACT | Cross-domain transaction-neutral/outbox boundary is documented and tested; production coordination remains open. |
| DATA-003 | PROVEN-CONTRACT | Outbox staging/value immutability tests directly cover the contract. |
| DATA-004 | PROVEN-CONTRACT | CAS revision behavior is directly tested and reused by workflow/job foundations. |
| DATA-005 | MISSING | Crash-safe migration rehearsal and backup verification are not evidenced. |
| DATA-006 | MISSING | Epoch-2 adoption and temporary schema cleanup are not evidenced. |
| DATA-007 | PROVEN-CONTRACT | `CROSSCUT-DATA-007-ATTACH.md`; immutable artifact manifests/spill metadata and `tests/test_crosscutting_artifacts.py`. |

### OPS

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| OPS-001 | PROVEN-CONTRACT | `WP9-OPS-001-003-006.md`; bounded operation context/correlation records and `tests/test_wp9_operations.py`. |
| OPS-002 | PROVEN-CONTRACT | Same evidence covers structured tracing and redact-before-export. |
| OPS-003 | PROVEN-CONTRACT | Same evidence covers liveness/readiness/dependency health states. |
| OPS-004 | PROVEN-CONTRACT | `WP9-OPS-004-005.md`; startup reconciliation classifications and `tests/test_wp9_reconciliation.py`. Durable repair execution remains open. |
| OPS-005 | PARTIAL | Graceful-drain intent/deadline contracts are tested, but actual admission stop and settling are not proven. |
| OPS-006 | PROVEN-CONTRACT | Bounded cardinality and redacted telemetry export are tested. |

### SEC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SEC-001 | PROVEN-CONTRACT | `WP9-SEC-001-002.md`; scoped credential handles, expiry/revocation, and `tests/test_wp9_credential_egress.py`. |
| SEC-002 | PROVEN-CONTRACT | Same evidence covers protocol/host/network restrictions and redirect-hop checks. |
| SEC-003 | PARTIAL | `WP9-SEC-003-006.md`; path/archive validation exists, but OS-level race resistance is explicitly not claimed. |
| SEC-004 | PROVEN-CONTRACT | Archive bounds, expansion ratios, links, and path checks are tested. |
| SEC-005 | PARTIAL | Extension provenance/quarantine exists in `CROSSCUT-EXT-001-005.md`, but SBOM/signature inventory is not complete. |
| SEC-006 | PARTIAL | Untrusted-boundary concepts exist, but end-to-end prompt-injection provenance labels are not evidenced. |
| SEC-007 | PROVEN-CONTRACT | `WP9-SEC-007-008-UPDATE.md`; bounded secret scanning and redaction tests. |
| SEC-008 | PROVEN-CONTRACT | Same evidence covers bounded decoder fuzz-harness behavior. |
| SEC-009 | MISSING | Same-user recovery and audit-file boundary claims are not sufficiently evidenced. |

### TRAIN

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| TRAIN-001 | PROVEN-CONTRACT | `WP7-TRAIN-001-007.md`; reproducible manifest identity and `tests/test_wp7_training_catalog.py`. |
| TRAIN-002 | MISSING | Exact qualified training dependency lock is not evidenced. |
| TRAIN-003 | MISSING | Dataset privacy/license/source/deduplication validation is not evidenced. |
| TRAIN-004 | PARTIAL | Promotion/evaluation gates exist, but a training-specific behavior/regression/latency/memory gate is not proven. |
| TRAIN-005 | PROVEN-CONTRACT | `WP7-TRAIN-005-009.md`; immutable deployment artifact identity and health-gated activation. |
| TRAIN-006 | PROVEN-CONTRACT | Attended deployment service behavior is tested. Durable active-route integration remains open. |
| TRAIN-007 | PROVEN-CONTRACT | Adapter catalog identity/task/project/personalization shape is tested. |
| TRAIN-008 | MISSING | Cheap-learning-first orchestration policy is not evidenced. |
| TRAIN-009 | PROVEN-CONTRACT | Explicit rollback and prior-route retention are tested. |

### UPDATE

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| UPDATE-001 | PARTIAL | `WP9-SEC-007-008-UPDATE.md` provides signed activation foundations, not the complete bounded updates domain/TUF state. |
| UPDATE-002 | MISSING | Cross-platform helper-process activation is not evidenced. |
| UPDATE-003 | MISSING | Exact sealed-runtime dependency contract verification is not evidenced. |
| UPDATE-004 | PARTIAL | Signed activation retains a prior route, but atomic platform activation and standalone recovery are not proven. |
| UPDATE-005 | MISSING | Signed release evidence package/SBOM/test publication is not evidenced. |

### DOC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| DOC-001 | PARTIAL | `docs/architecture/README.md` and this audit improve indexing, but an authoritative complete map is not yet demonstrated. |
| DOC-002 | MISSING | Superseded-document labels across SPEC-5/runbook/older active-looking documents are not fully audited. |
| DOC-003 | MISSING | One globally unique ADR namespace is not evidenced. |
| DOC-004 | MISSING | The required focused contract-document set is not evidenced as complete/current. |
| DOC-005 | MISSING | Generated tool/command/event/configuration references are not evidenced. |
| DOC-006 | PARTIAL | The evidence ledger and this audit establish process, but no requirement has a verified ledger record in this baseline. |
| DOC-007 | PARTIAL | Several evidence docs correctly state limitations, but a complete stale-promise sweep is still required. |

## Safe checkbox candidates

**None.** `PROVEN-CONTRACT` means the named slice has direct tests and an
evidence document; it does not satisfy the formal completion bar by itself.
The authoritative ledger has no `verified` records, and the current evidence
contains explicit gaps in production wiring, durable persistence, platform
activation, end-to-end acceptance, or full requirement scope. No formal
checkbox should be changed based on this audit alone.

The strongest next promotion candidates, after adding their missing integration
and durable evidence in a single reviewed change, are the contract-heavy
subsets of SESSION-001/003/006/007/009/010, LOOP-001/002/003/004/005,
CTX-001–010, SKILL-001–005, API-001–006, and SEC-001/002/004/007/008. They are
candidate targets, not safe candidates today.

## Required follow-up evidence

1. Add requirement-specific ledger revisions with real `baseline_sha`,
   `verified_sha`, focused/regression evidence, and limitations only after the
   end-to-end requirement is met.
2. Close the explicit partial/missing rows above, prioritizing session
   persistence/query, unified agent/job integration, execution adapters,
   update activation, mobile/schema surfaces, and documentation authority.
3. Run the full acceptance, migration rehearsal, recovery, security, platform,
   soak, and performance matrices; focused tests alone cannot support the
   final checklist.
4. Re-run this audit and the formal evidence checker before changing any
   checkbox. A checked item must be changed in the same reviewed change that
   adds its verified evidence.

