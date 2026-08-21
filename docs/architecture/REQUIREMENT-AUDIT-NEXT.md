# Requirement evidence audit — next formalization pass

Date: 2026-08-20  
Audited branch: `agent/wp1-execution-status`  
Audit baseline: current branch HEAD `df10f7c` (2026-08-20 refresh)
Scope: SESSION/LOOP/SEAM/CTX/REPO/SKILL/AGENT/JOB/TOOL/EXEC/MEM/EVAL/MODEL/API/DATA/OPS/SEC/TRAIN/UPDATE/DOC

## Decision summary

This is an evidence audit, not a formal checklist update. The master
specification and its formal checkboxes were not edited.

- Requested scope: **163 IDs** across 19 families.
- Refresh promotions: **8** rows gained direct focused-test and evidence-document support since the prior audit.
- Current classification: **163 PROVEN-CONTRACT / 0 PARTIAL / 0 MISSING**.
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
| SESSION-002 | PROVEN-CONTRACT | `REMAINING-SESSION-002-006.md`; `tests/test_remaining_session_002_006.py` directly covers SQLite append-only capture and restart-visible event state. |
| SESSION-003 | PROVEN-CONTRACT | `WP2-SESSION-003.md`; `sonder_runtime/domain/session/events.py`; `tests/test_session_event_schema.py`. |
| SESSION-004 | PROVEN-CONTRACT | `REMAINING-SESSION-002-006.md`; `tests/test_remaining_session_002_006.py` directly covers request, UI, tool, and model-visible event capture. |
| SESSION-005 | PROVEN-CONTRACT | `REMAINING-SESSION-002-006.md`, `REMAINING-SESSION-008-SEAM-006.md`; focused tests directly cover deterministic replay, transcript/projection reconstruction, query, export, redaction, and integrity. |
| SESSION-006 | PROVEN-CONTRACT | `WP2-SESSION-006.md`; `sonder_runtime/application/session/fork.py`; `tests/test_session_fork.py`. |
| SESSION-007 | PROVEN-CONTRACT | `WP2-SESSION-007.md`; `sonder_runtime/application/session/repair.py`; `tests/test_session_repair.py`. |
| SESSION-008 | PROVEN-CONTRACT | `REMAINING-SESSION-008.md`, `REMAINING-SESSION-008-SEAM-006.md`; `tests/test_remaining_session_query_export.py` directly covers bounded query, cursor binding, transcript, redaction, export, and integrity behavior. |
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
| LOOP-006 | PROVEN-CONTRACT | `REMAINING-LOOP-006-008.md`, `REMAINING-JOB-004-LOOP-006-PROCESS-TREE.md`; `tests/test_remaining_loop_control.py` and `tests/test_process_tree_supervisor.py` directly cover typed cancellation propagation, cleanup conformance evidence, and fail-closed process/provider cleanup. Full provider wiring remains outside this contract slice. |
| LOOP-007 | PROVEN-CONTRACT | `REMAINING-LOOP-007-008.md`; `tests/test_remaining_loop_007_008.py` directly covers durable retry evidence retention and reconciliation classification. Transport retry execution remains outside this contract slice. |
| LOOP-008 | PROVEN-CONTRACT | `REMAINING-LOOP-007-008.md`; `tests/test_remaining_loop_007_008.py` directly covers SQLite idempotency, outbox atomicity, recreation, and fingerprint conflict handling. |

### SEAM

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SEAM-001 | PROVEN-CONTRACT | `WP3-SEAM-001.md`; `sonder_runtime/application/ports/model_gateway_contract.py`; `tests/test_model_gateway_contract_wp3.py`. |
| SEAM-002 | PROVEN-CONTRACT | `WP3-SEAM-002.md`; tool registry/executor ports and `tests/test_wp3_seam002_tool_contract.py`. Adapter migration remains outside the slice. |
| SEAM-003 | PROVEN-CONTRACT | `WP3-SEAM-003.md`; filesystem port and `tests/test_filesystem_port_wp3.py`. Caller/provider migration remains unproved. |
| SEAM-004 | PROVEN-CONTRACT | `WP3-SEAM-004.md`; execution-world port and `tests/test_execution_world_port.py`. Concrete adapter unification remains unproved. |
| SEAM-005 | PROVEN-CONTRACT | `WP3-SEAM-005.md`; sandbox port and `tests/test_sandbox_provider_port.py`. A real sandbox security claim is not made. |
| SEAM-006 | PROVEN-CONTRACT | `REMAINING-SESSION-008-SEAM-006.md`; `tests/test_remaining_session_query_export.py` directly covers the bounded SessionRepository/SessionQueryEngine query and export seam. |
| SEAM-007 | PROVEN-CONTRACT | `WP3-SEAM-007.md`; compaction port and `tests/test_wp3_seam007_compaction.py`. |
| SEAM-008 | PROVEN-CONTRACT | `WP3-SEAM-008.md`; skill registry contract and `tests/test_skill_registry.py`. |
| SEAM-009 | PROVEN-CONTRACT | `WP3-SEAM-009.md`; provider-neutral child contract exists, but a production provider is not proven. |
| SEAM-010 | PROVEN-CONTRACT | `WP3-SEAM-010.md`; job/workflow ports and `tests/test_wp3_seam010_jobs.py`. Existing stores/transports remain outside the slice. |
| SEAM-011 | PROVEN-CONTRACT | `WP3-SEAM-011.md`; web/credential ports and `tests/test_wp3_seam011_web.py`. |
| SEAM-012 | PROVEN-CONTRACT | `WP3-SEAM-012-013.md`; artifact port and focused contract tests. |
| SEAM-013 | PROVEN-CONTRACT | `WP3-SEAM-012-013.md`; telemetry port and focused contract tests. |
| SEAM-014 | PROVEN-CONTRACT | `REMAINING-SEAM-014.md`; `tests/test_remaining_specialized_providers.py` directly covers publication, lifecycle, cancellation, bounded cleanup, and rollback through the specialized provider wiring API. |
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
| REPO-004 | PROVEN-CONTRACT | `REMAINING-REPO-004-007.md`; `tests/test_remaining_repo_lsp_multiroot.py` directly covers provider-neutral live LSP session lifecycle, bounded results, root identity validation, and deterministic cleanup. |
| REPO-005 | PROVEN-CONTRACT | `WP4-REPO-001-003-005.md`; digest-bound evidence contract and tests. |
| REPO-006 | PROVEN-CONTRACT | `WP4-REPO-004-006-007.md`; progressive symbol expansion contract and `tests/test_repository_navigation.py`. |
| REPO-007 | PROVEN-CONTRACT | `REMAINING-REPO-004-007.md`; `tests/test_remaining_repo_lsp_multiroot.py` directly covers bounded multi-repository reads, independent root revisions, cross-root evidence rejection, and write authorization. |

### SKILL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SKILL-001 | PROVEN-CONTRACT | `WP4-SKILL-001-002.md`; registry discovery and `tests/test_wp4_skill_registry.py`. |
| SKILL-002 | PROVEN-CONTRACT | `WP4-SKILL-001-002.md`; progressive metadata/content disclosure and focused tests. |
| SKILL-003 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; refresh/digest implementation and `tests/test_skill_refresh_plugin_manifest.py`. |
| SKILL-004 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; source/trust/version policy fields and focused tests. |
| SKILL-005 | PROVEN-CONTRACT | `WP4-SKILL-003-004-005.md`; malformed/repeated-failure quarantine contract and focused tests. |
| SKILL-006 | PROVEN-CONTRACT | `REMAINING-SKILL-006-MEM-008.md`; `tests/test_remaining_procedural_publication.py` directly covers durable catalog revisions, active-skill publication, rollback, disablement, and failure recovery. |

### AGENT

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| AGENT-001 | PROVEN-CONTRACT | `WP5-AGENT-001.md`; registry adapter and `tests/test_wp5_fleet_autopilot.py`. Full unified service integration remains open. |
| AGENT-002 | PROVEN-CONTRACT | `WP5-AGENT-002.md`; Workbench/review adapters and `tests/test_wp5_workbench_review.py`. |
| AGENT-003 | PROVEN-CONTRACT | `WP5-AGENT-003.md`; roles/presets/budgets and `tests/test_wp5_roles_presets_budgets.py`. |
| AGENT-004 | PROVEN-CONTRACT | `REMAINING-AGENT-004-008-009.md`; `tests/test_remaining_agent_004_008_009.py` directly covers the typed built-in preset catalog, including `researcher`. |
| AGENT-005 | PROVEN-CONTRACT | `REMAINING-AGENT-005-JOB-002-004.md`; `tests/test_remaining_agent_005_job_integration.py` directly covers restart-persistent lineage and bounded read-only descendant/operator queries. |
| AGENT-006 | PROVEN-CONTRACT | `REMAINING-AGENT-006.md`; `tests/test_remaining_durable_subagents.py` directly covers SQLite lineage/checkpoint CAS, restart resume, durable cancellation, and orphan recovery. |
| AGENT-007 | PROVEN-CONTRACT | `WP5-AGENT-003.md`, `WP5-SUBAGENT-001.md`; role/depth/count/concurrency budget foundations are tested, but full enforcement integration is open. |
| AGENT-008 | PROVEN-CONTRACT | `REMAINING-AGENT-004-008-009.md`; `tests/test_remaining_agent_004_008_009.py` directly covers read/write workspace containment and parent-context isolation. |
| AGENT-009 | PROVEN-CONTRACT | `REMAINING-AGENT-004-008-009.md`; `tests/test_remaining_agent_004_008_009.py` directly covers port-backed delegation, accepted events, bounded result evidence, and lineage validation. |
| AGENT-010 | PROVEN-CONTRACT | `REMAINING-AGENT-010.md`; `tests/test_remaining_agent_010.py` directly covers the explorer → architect → editor → verifier → reviewer → integrator path, preset routing, durable lineage binding, evidence-backed terminal state, failure short-circuiting, and workspace enforcement. Durable child records remain provider-owned. |

### JOB

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| JOB-001 | PROVEN-CONTRACT | `WP5-JOB-001.md`; typed dependency-ordered generic jobs and `tests/test_wp5_generic_jobs.py`. |
| JOB-002 | PROVEN-CONTRACT | `REMAINING-AGENT-005-JOB-002-004.md`; `tests/test_remaining_agent_005_job_integration.py` directly covers durable start/list/poll/stream linkage and the SQLite registry boundary. |
| JOB-003 | PROVEN-CONTRACT | `REMAINING-AGENT-005-JOB-002-004.md`; `tests/test_remaining_agent_005_job_integration.py` directly covers restart reconciliation, orphan recovery, and truthful incomplete-cleanup handling. |
| JOB-004 | PROVEN-CONTRACT | `REMAINING-JOB-004-LOOP-006-PROCESS-TREE.md`; `tests/test_process_tree_supervisor.py` directly covers Windows tree termination, POSIX process-group enforcement, unsupported/incomplete outcomes, and typed cleanup receipts. End-to-end wiring of every execution provider remains outside this contract slice. |
| JOB-005 | PROVEN-CONTRACT | `REMAINING-EXEC-001-006.md`; bounded pages, cursors, watermarks, truncation, and spill references are tested. Durable integration remains open. |

### TOOL

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| TOOL-001 | PROVEN-CONTRACT | `CROSSCUT-TOOL-001-005.md`; `tests/test_crosscutting_tool_gateway.py` verifies one gateway pipeline. |
| TOOL-002 | PROVEN-CONTRACT | Same gateway evidence verifies schema, scope, permission, approval, deadline, cancellation, redaction, and receipt order. |
| TOOL-003 | PROVEN-CONTRACT | `REMAINING-TOOL-003-005.md`; `tests/test_remaining_tool_policy.py` directly covers bounded path/host/resource/preset/workspace/authority matching and fail-closed decisions. |
| TOOL-004 | PROVEN-CONTRACT | Gateway tests cover allow/deny/approval modes; session/project persistence is not proven. |
| TOOL-005 | PROVEN-CONTRACT | `REMAINING-TOOL-003-005.md`; `tests/test_remaining_tool_policy.py` directly covers independent immutable startup authorities and fail-closed authority requirements. |
| TOOL-006 | PROVEN-CONTRACT | `REMAINING-TOOL-006-API-007-008.md`, `REMAINING-DOC-001-007.md`; focused catalog/artifact/freshness tests directly cover MCP/OpenAI/CLI/client schemas, permissions, conformance fixtures, documentation projection, and CI drift rejection. |
| TOOL-007 | PROVEN-CONTRACT | Gateway receipt/redaction tests prove the contract; durable audit storage is not proven. |

### EXEC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| EXEC-001 | PROVEN-CONTRACT | `REMAINING-EXEC-001-006.md`; shared world bindings and `tests/test_remaining_execution_world.py`. |
| EXEC-002 | PROVEN-CONTRACT | `REMAINING-EXEC-002-005.md`; `tests/test_remaining_execution_world_defaults.py` directly covers the guarded container default, image identity, no-host-fallback behavior, and fail-closed operations. |
| EXEC-003 | PROVEN-CONTRACT | Remaining execution tests cover terminal/job lifecycle, reconnection, bounded reads, and watermarks; a concrete persistent terminal adapter remains open. |
| EXEC-004 | PROVEN-CONTRACT | Remaining execution evidence covers digest-bound spill references and bounded output. Durable spill store wiring remains open. |
| EXEC-005 | PROVEN-CONTRACT | `REMAINING-EXEC-002-005.md`; `tests/test_remaining_execution_world_defaults.py` directly covers HTTPS endpoint, worker identity, capability matching, cleanup, and fail-closed remote operations. |
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
| MEM-008 | PROVEN-CONTRACT | `REMAINING-SKILL-006-MEM-008.md`; `tests/test_remaining_procedural_publication.py` directly covers memory-to-skill provenance linkage, held-out evidence, promotion provenance, active-skill integration, and rollback. |

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
| MODEL-003 | PROVEN-CONTRACT | `REMAINING-MODEL-001-010.md`; `LogicalRole` provides stable logical roles without selecting a transport. |
| MODEL-004 | PROVEN-CONTRACT | `WP7-MODEL-001.md`; measured calibration profiles and `tests/test_wp7_calibration.py`. |
| MODEL-005 | PROVEN-CONTRACT | `WP7-MODEL-002.md`; capability profiles and routing tests. |
| MODEL-006 | PROVEN-CONTRACT | `REMAINING-MODEL-001-010.md`; `ModelParameters` preserves total and active MoE parameter counts for separate residency/compute truth. |
| MODEL-007 | PROVEN-CONTRACT | `REMAINING-MODEL-007.md`; `tests/test_remaining_model_007.py` directly covers uncertainty/verifier triggers, request-scoped routes, bounds, provenance, outcomes, and event emission. |
| MODEL-008 | PROVEN-CONTRACT | `REMAINING-MODEL-001-010.md`; `RoleBudgetBook` provides independent immutable-by-snapshot role budgets. Full caller enforcement remains open. |
| MODEL-009 | PROVEN-CONTRACT | `REMAINING-MODEL-001-010.md`; routability requires explicit `ready` or `degraded` provider health. |
| MODEL-010 | PROVEN-CONTRACT | `REMAINING-MODEL-001-010.md`; `NpuBoundary` separates detection, runtime availability, and provider binding. |

### API

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| API-001 | PROVEN-CONTRACT | `WP8-API-001-002.md`; typed event vocabulary and `tests/test_wp8_protocol_streams.py`. |
| API-002 | PROVEN-CONTRACT | Same evidence covers snapshots, monotonic sequence numbers, resume watermarks, and bounded batches. |
| API-003 | PROVEN-CONTRACT | `WP8-API-003.md`; MCP negotiation/legacy declaration and focused compatibility tests. Network/provider integration is intentionally absent. |
| API-004 | PROVEN-CONTRACT | `WP8-API-004.md`; OpenAI compatibility mapping and focused tests. |
| API-005 | PROVEN-CONTRACT | `WP8-API-005.md`; bounded editor/agent envelopes and safe rule exchange tests. |
| API-006 | PROVEN-CONTRACT | `WP8-API-006.md`; operator control-plane snapshot and focused tests. |
| API-007 | PROVEN-CONTRACT | `REMAINING-API-007-008.md`, `REMAINING-TOOL-006-API-007-008.md`; `tests/test_remaining_client_schema.py` and `tests/test_mobile_parity_wire.py` directly cover provider-neutral mobile reconnect, bounded resume, snapshots, continuation, and fail-closed validation. |
| API-008 | PROVEN-CONTRACT | `REMAINING-API-007-008.md`, `REMAINING-TOOL-006-API-007-008.md`; `tests/test_remaining_client_schema.py` directly cover runtime-derived client/SDK projections, digest freshness, and schema refresh before replay. |

### DATA

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| DATA-001 | PROVEN-CONTRACT | `REMAINING-DATA-001.md`; `tests/test_remaining_domain_ownership.py` directly covers one-to-one per-domain SQLite ownership, canonical alias collision rejection, and declaration-drift validation. |
| DATA-002 | PROVEN-CONTRACT | Cross-domain transaction-neutral/outbox boundary is documented and tested; production coordination remains open. |
| DATA-003 | PROVEN-CONTRACT | Outbox staging/value immutability tests directly cover the contract. |
| DATA-004 | PROVEN-CONTRACT | CAS revision behavior is directly tested and reused by workflow/job foundations. |
| DATA-005 | PROVEN-CONTRACT | `REMAINING-DATA-005-006.md`; `tests/test_remaining_data_005_006.py` directly covers disposable crash rehearsal, backup/restore proof, fault boundaries, and source immutability. |
| DATA-006 | PROVEN-CONTRACT | `REMAINING-DATA-005-006.md`; `tests/test_remaining_data_005_006.py` directly covers read-only epoch-2 adoption, receipt validation, and temporary-schema detection. |
| DATA-007 | PROVEN-CONTRACT | `CROSSCUT-DATA-007-ATTACH.md`; immutable artifact manifests/spill metadata and `tests/test_crosscutting_artifacts.py`. |

### OPS

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| OPS-001 | PROVEN-CONTRACT | `WP9-OPS-001-003-006.md`; bounded operation context/correlation records and `tests/test_wp9_operations.py`. |
| OPS-002 | PROVEN-CONTRACT | Same evidence covers structured tracing and redact-before-export. |
| OPS-003 | PROVEN-CONTRACT | Same evidence covers liveness/readiness/dependency health states. |
| OPS-004 | PROVEN-CONTRACT | `WP9-OPS-004-005.md`; startup reconciliation classifications and `tests/test_wp9_reconciliation.py`. Durable repair execution remains open. |
| OPS-005 | PROVEN-CONTRACT | `REMAINING-OPS-005.md`; `tests/test_remaining_graceful_drain.py` and `tests/test_remaining_admission_gate.py` directly cover ordered drain barriers, admission stop, settling, cleanup truth, and deadline failure. |
| OPS-006 | PROVEN-CONTRACT | Bounded cardinality and redacted telemetry export are tested. |

### SEC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| SEC-001 | PROVEN-CONTRACT | `WP9-SEC-001-002.md`; scoped credential handles, expiry/revocation, and `tests/test_wp9_credential_egress.py`. |
| SEC-002 | PROVEN-CONTRACT | Same evidence covers protocol/host/network restrictions and redirect-hop checks. |
| SEC-003 | PROVEN-CONTRACT | `REMAINING-SEC-003.md`; `tests/test_remaining_race_resistance.py` directly covers truthful platform capability, symlink/escape rejection, bounded targets, and fail-closed Windows behavior. |
| SEC-004 | PROVEN-CONTRACT | Archive bounds, expansion ratios, links, and path checks are tested. |
| SEC-005 | PROVEN-CONTRACT | `REMAINING-SEC-005.md`; `tests/test_remaining_extension_provenance.py` directly covers signature/trust records, deterministic SBOM identity, tamper detection, and quarantine admission. |
| SEC-006 | PROVEN-CONTRACT | `REMAINING-SEC-006.md`; `tests/test_remaining_prompt_provenance.py` directly covers untrusted labels, request binding, redacted events, replay, tamper detection, and fail-closed malformed input. |
| SEC-007 | PROVEN-CONTRACT | `WP9-SEC-007-008-UPDATE.md`; bounded secret scanning and redaction tests. |
| SEC-008 | PROVEN-CONTRACT | Same evidence covers bounded decoder fuzz-harness behavior. |
| SEC-009 | PROVEN-CONTRACT | `REMAINING-SEC-009.md`; `tests/test_remaining_sec_009.py` directly covers bounded owner/path recovery artifacts, chained audit integrity, tamper detection, and the explicit tamper-evident-only limitation. Same-user storage is not claimed as an independent security boundary. |

### TRAIN

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| TRAIN-001 | PROVEN-CONTRACT | `WP7-TRAIN-001-007.md`; reproducible manifest identity and `tests/test_wp7_training_catalog.py`. |
| TRAIN-002 | PROVEN-CONTRACT | `REMAINING-TRAIN-002-003-004-008.md`; `tests/test_remaining_training_002_003_004_008.py` directly cover exact dependency records, missing/extra/duplicate/mismatch rejection, and environment binding. |
| TRAIN-003 | PROVEN-CONTRACT | `REMAINING-TRAIN-002-003-004-008.md`; `tests/test_remaining_training_002_003_004_008.py` directly cover privacy, source/license, deduplication, contamination, and train/eval separation gates. |
| TRAIN-004 | PROVEN-CONTRACT | `REMAINING-TRAIN-002-003-004-008.md`; `tests/test_remaining_training_002_003_004_008.py` directly cover behavior, regression, latency, memory, context, and tool-use evaluation gates. |
| TRAIN-005 | PROVEN-CONTRACT | `WP7-TRAIN-005-009.md`; immutable deployment artifact identity and health-gated activation. |
| TRAIN-006 | PROVEN-CONTRACT | Attended deployment service behavior is tested. Durable active-route integration remains open. |
| TRAIN-007 | PROVEN-CONTRACT | Adapter catalog identity/task/project/personalization shape is tested. |
| TRAIN-008 | PROVEN-CONTRACT | `REMAINING-TRAIN-002-003-004-008.md`; `tests/test_remaining_training_002_003_004_008.py` directly cover ordered cheap-learning methods, first-reliable selection, and weight-training fallback. |
| TRAIN-009 | PROVEN-CONTRACT | Explicit rollback and prior-route retention are tested. |

### UPDATE

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| UPDATE-001 | PROVEN-CONTRACT | `REMAINING-UPDATE-001-005.md`; `tests/test_remaining_update_001_005.py` directly covers bounded update lifecycle ordering, digest/health/activation/rollback failure paths, history bounds, and TUF-like metadata links, expiry, signer validation, and target bounds. Network and trust adapters remain injected. |
| UPDATE-002 | PROVEN-CONTRACT | `REMAINING-UPDATE-002-004.md`; `tests/test_remaining_update_002_004.py` directly covers platform-neutral helper activation requests without platform-specific execution. |
| UPDATE-003 | PROVEN-CONTRACT | `REMAINING-UPDATE-002-004.md`; `tests/test_remaining_update_002_004.py` directly covers exact sealed dependency equality, missing/extra entries, and digest tampering. |
| UPDATE-004 | PROVEN-CONTRACT | `REMAINING-UPDATE-002-004.md`; `tests/test_remaining_update_002_004.py` directly covers atomic activation rollback, standalone recovery evidence, and explicit incomplete recovery. |
| UPDATE-005 | PROVEN-CONTRACT | `REMAINING-UPDATE-001-005.md`; `tests/test_remaining_update_001_005.py` directly covers deterministic signed publication of manifest hashes, SBOM, test results, migration/rollback evidence, complete target verification, and tamper rejection. External publication transport remains outside this contract slice. |

### DOC

| ID | Finding | Current evidence or missing proof |
|---|---|---|
| DOC-001 | PROVEN-CONTRACT | `REMAINING-DOC-001-007.md`; `tests/test_remaining_doc_001_005.py` directly covers the authority index and deterministic architecture map. |
| DOC-002 | PROVEN-CONTRACT | `REMAINING-DOC-001-007.md`; `tests/test_remaining_doc_001_005.py` directly covers historical/superseded labeling authority. |
| DOC-003 | PROVEN-CONTRACT | `REMAINING-DOC-001-007.md`; `tests/test_remaining_doc_001_005.py` directly covers the ADR namespace rule. |
| DOC-004 | PROVEN-CONTRACT | `REMAINING-DOC-001-007.md`; `tests/test_remaining_doc_001_005.py` directly covers the focused contract inventory. |
| DOC-005 | PROVEN-CONTRACT | `REMAINING-DOC-001-007.md`; `tests/test_remaining_doc_001_005.py` directly covers generated command/tool/event/configuration references and freshness. |
| DOC-006 | PROVEN-CONTRACT | `REMAINING-DOC-006-007.md`; `scripts/check_evidence_documents.py` and `tests/test_evidence_document_consistency.py` directly cover bounded evidence-document validation, focused-test link resolution, and explicit limitation/verification disclosures. |
| DOC-007 | PROVEN-CONTRACT | `REMAINING-DOC-006-007.md`, `REMAINING-DOC-001-007.md`; the evidence checker and `tests/test_evidence_document_consistency.py` cover the current stale-promise inventory and reject unresolved focused-test references. |

## Remaining partial rows

There are no remaining `PARTIAL` or `MISSING` rows in the 163-row scoped audit.
These classifications are contract evidence
only and do not promote any formal checkbox or ledger record.

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
2. Add requirement-specific ledger revisions with real `baseline_sha`,
   `verified_sha`, focused/regression evidence, and limitations only after
   end-to-end verification exists; this audit does not promote ledger records.
3. Run the full acceptance, migration rehearsal, recovery, security, platform,
   soak, and performance matrices; focused tests alone cannot support the
   final checklist.
4. Re-run this audit and the formal evidence checker before changing any
   checkbox. A checked item must be changed in the same reviewed change that
   adds its verified evidence.
