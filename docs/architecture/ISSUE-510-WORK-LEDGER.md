# Issue 510 execution ledger

Updated 2026-09-24. This is an implementation and verification work ledger for
[#510](https://github.com/Krilliac/Sonder-runtime/issues/510), including the
2026-09-24 strategy/recovery program and the associated ten-item defect audit.
Baseline: `a6a082859d4d3d4ec80cf6497bd37d6052bba5e9`.

The [master specification](SONDER-MASTER-IMPLEMENTATION-SPEC.md) and append-only
[requirement evidence](evidence/requirements.jsonl) remain the requirement/status
authorities. A local passing test, an observation adapter, or an entry below does
not make a complete requirement verified. No unchecked master item is checked by
this ledger. Existing implementations are extended instead of being duplicated.

## Defect audit

| Item | Implementation and meaningful verification | Qualification |
|---|---|---|
| W1 Artifact signature/publisher trust | Windows signable artifacts reject unavailable verification; exact normalized publisher organization matching; verifier environment stripped; non-Windows results explicitly disclose absent signature verification. `tests/test_artifact_fetch_tools.py`, `tests/test_runtime_artifact_adapters.py`. | Implemented; real signed Windows positive fixture still required. |
| W2 Root/concurrency counting | Root anchor is excluded from child occupancy; reused reservations counted once; forged provider-root metadata rejected. `tests/test_child_lifecycle_boundaries.py`, `tests/test_continuation_worker_registry.py`. | Local tests pass; atomic multi-process admission is a separate follow-up. |
| W3 Cancellation and reserved workers | Transactional cancellation fence prevents late success, pre-start cancellation terminalizes the record, resume clears stale verification. Provider launch failure releases only a newly created unstarted row with its expected revision. | Implemented; race and reused-reservation canaries pass. |
| W4 Wall budget | Child deadline and measured elapsed time reject success after budget exhaustion. | Implemented; deterministic late-success test. |
| W5 Durable outcome authority | Delegation verification compares the full canonical terminal result, including status, output, error and usage. Identical receipts are idempotent; conflicting receipts fail. | Implemented; forged-success and resumed-result regressions pass. |
| W6 Compiler-feedback repair | Next codegen candidate receives bounded prior source, normalized diagnostics and progress; explicit multi-route policy admits a scoped critic then a distinct model; incomplete evidence and critic failure degrade safely; best candidate retained. | Implemented and unit/integration tested; live model benefit remains unmeasured. |
| W7 Runtime closure / #550 | Inventory and byte/count bounds precede hashing; scan/hash replacement detected; full closure still hashed. Canonical Windows installer can provision a separate pinned `venv-managed`; host selects and validates its bound profile. | Linux contract tests pass. Native Windows installer/launch qualification is required; no arbitrary new production size/time cap. |
| W8 Release dependency gate | Tagged release depends on integrity, Flutter analysis and reusable exact-tag Python CI, including smoke and the Windows focused suite. Existing required `tests` context retained. | Workflow structure and local release tests pass; tagged publication itself is not exercised. |
| W9 Release archive validation | Require real platform binaries, nested payload manifests and digests, build identity and bounded archive structure. | Four existing platform artifacts accepted; malformed and incomplete archive canaries pass. |
| W10 Strategy convergence | Pure contracts/controller, durable observations and production adapters, bounded tools and evidence-backed memory are being integrated through existing ports. | Experimental. The full program definition of done is not yet met. |

The workflow budget follow-up also fixes explorer/architect/editor stages being
incorrectly nested under the previous role's smaller budget. Every sequential
stage retains the original durable parent and actual lineage depth.

## Original issue sections

| Section | Canonical implementation / retained evidence | Current work and remaining qualification |
|---|---|---|
| 1 Lossless archives and selective eviction | Session archive, `application/compaction`, CTX/COMPACT retention; merged #547. | Preserve archive references, objective/constraints and failed-attempt evidence. Strategy context uses typed references, not transcript copies. |
| 2 Resumable worker registry | `application/worker_registry/continuation.py`, `application/subagents/durable_continuation.py`. | Cancellation, terminal-result and launch-cleanup fixes above; restart/reuse canaries retained. |
| 3 Productive parallelism | `domain/adaptive_concurrency.py`, ownership scheduler from #552; AGENT-007. | Atomic resource reservations and distinct hypothesis admission are separate slices; local locks alone are not a cross-process guarantee. |
| 4 Worker execution contracts | AGENT-009 / #541, `application/agents/delegation_service.py`. | Canonical result binding, scoped critic context and inherited budgets. |
| 5 Deterministic workflow gates | Delegation role workflow, artifact readiness barrier, EVAL proposal lifecycle. | PR #546 owns EVAL-006/007 and is reconciled before extension. |
| 6 Critical runtime guards | [Guard inventory](evidence/ISSUE-510-GUARD-INVENTORY-2026-09-23.md), #553 no-progress guards. | New guards need deliberate-trigger canaries. Request-rate, top-tier spawn accounting and batching remain distinct gaps. |
| 7 Guard canaries | `tests/test_issue510_existing_guard_canaries.py`, verification/no-progress suites. | Existing canaries retained; concurrency, forged outcome, cancellation and signature refusal added. |
| 8 Evidence-based learning ladder | MEM-003, merged #518/#525 authenticated observations. | Strategy experience extends canonical memory UoW and LearningLadder; ordinary successful runs never authorize policy promotion. |
| 9 Skill TDD | SKILL-006 / MEM-008, existing skill promotion tests. | Procedure generation still requires held-out evaluation and promotion; no automatic conversion from a single strategy success. |
| 10 Serializable checkpoints | SESSION-004/005/007/009, LOOP-008, merged #518/#523. | Strategy uses RuntimeCheckpointRepository CAS and sealing. Observation DB is not an effect journal and cannot authorize replay. |
| 11 Hybrid memory | MEM-004/005, merged #538; authoritative facts and scoped indexes. | Strategy memory carries provenance, attribution, failed-reuse decay and bounded context references. |
| 12 Isolated harness evolution | SELFMOD-002 / #517/#519, `scripts/selfmod_low_integrity.py`. | Unsupported-host auto execution now refuses; auto approval requires persisted host isolation evidence. Low MIC does not establish confidentiality or network isolation. |

## Strategy/recovery program

The series IDs below are from issue comment
[5808455090](https://github.com/Krilliac/Sonder-runtime/issues/510#issuecomment-5808455090).
“Implemented” identifies a concrete mechanism, not full rollout acceptance.

| Series | Implementation / status | Remaining acceptance |
|---|---|---|
| A1 Contracts | Implemented in `domain/strategy/models.py`: bounded typed attempts, signatures, references, resource usage and budgets. | Integrate every host producer; no raw payload duplication. |
| A2 Failure vocabulary | Typed source-independent failure classes and conservative host safety properties. | Every host's source-specific projection must retain its evidence. |
| A3 Progress | Typed comparable vectors; incomplete/scope-mismatched evidence is incomparable; regressions take precedence. | General research/debugging projections and measured accuracy. |
| A4 Trace | Existing sealed checkpoint port; immutable attempt IDs; CAS; restart; exact attempt charge; budgets cannot expand. Codegen/Autopilot callers exist behind explicit opt-in. | Remaining host observations and crash cuts; native secure-key qualification. |
| B1 Feedback repair | Codegen production loop passes actual previous candidate and compiler diagnostics. | Live compiler/model quality evaluation. |
| B2 Scoped critic | Critic receives source/task/verifier facts and constraints, excluding implementer rationale. | Measure marginal critic benefit with held-out ablations. |
| B3 Model rotation | Explicit route policy; independent resolved model; bounded escalation following repeated comparable failure. | Measured capability eligibility and role-resource qualification. |
| B4 Best candidate | Improving/verified candidate retained; no blind overwrite on later regression. | Cross-host candidate restoration. |
| C1 Controller | Pure deterministic bounded decisions; effect uncertainty and host policy precede replay/repair. Success reaches the host completion gate even on the final budgeted attempt. | Authority remains with existing host gates; promoted active recovery needs equivalence evidence. |
| C2 Autopilot | Production observe adapter and durable attempt backfill. | Active shared-policy migration and fault matrix. |
| C3 Workbench | Existing live agent/effect/context boundaries retained. | Shared observer and bounded rollout integration in progress. |
| C4 Fleet | Existing scheduler and artifact fan-in retained. | Shared observations/shadow comparison and policy-bounded canary integration in progress. |
| D1 Structured results | Existing AGENT-009 plus exact durable terminal result binding. | Typed descendant proposal/evidence extensions. |
| D2 Reservations | Child ceilings inherit root resources; lifecycle fixes prevent reservation leakage. | Aggregate transactional sibling/depth accounting and PostgreSQL native qualification. |
| D3 Specialist proposals | Planned extension of the canonical delegation service. | Host-admitted bounded recursion and restart canaries. |
| D4 Hypotheses | Existing ownership-aware scheduler is the execution authority. | Distinct speculative identities and production admission. |
| D5 Evidence synthesis | Existing verified child result and readiness contracts. | Join only complete validated evidence, deterministic verdict and diagnosable exhausted search. |
| E1 Conformance battery | Existing chat/structured/cancellation battery retained. | Broader protocol probes; unsupported/unmeasured cases remain unknown. |
| E2 Profile persistence | Existing conformance evidence store. | Bind model digest/quantization, backend/version, template/tokenizer, context and hardware. |
| E3 Eligibility | Existing CapabilityRouter seam. | Reject unknown/stale/failed/mismatched evidence on production strict route. |
| E4 Escalation | Bounded codegen policy and existing logical route tiers. | Recent measured capability and separate role budgets. |
| E5 Attribution | Strategy observations include resolved route and measured usage. | Evaluation of whether escalation helped, including negative controls. |
| F1 ToolSearch | Implemented bounded deterministic summary search over an immutable, host-granted inventory. | Additional host surfaces beyond native MCP. |
| F2 Lazy schemas | `mcp --native --progressive-tools` exposes search/load tools, loads at most eight schemas and refuses calls before visibility. | Broader model request integration and resource evaluation. |
| F3 Skills | Existing ProgressiveSkillRegistry / LiveAgentContextProducer is summary-first and lazy. | Strategy-selected procedural skills still cross current policy/TDD gates. |
| F4 Schema replay identity | Loaded schema manifest binds inventory and selected schemas; typed request carries immutable selection; durable tool audit records it. | Non-typed compatibility tools do not yet share all durable receipt behavior. |
| F5 Cache telemetry | CTX-009, merged #531/#537/#545, selected schema identity in stable prefix. | Measure native progressive discovery's real prefix reuse. |
| G1 Experience | Content-free projection of sealed strategy attempts into canonical memory UoW. | Cross-host crash recovery integration. |
| G2 Failure retrieval | Scope/failure/family/language/verifier filters; bounded ContextPlanner projection. | Held-out irrelevant-memory controls. |
| G3 Attribution | Selected strategy-memory reference required for reuse credit; failed reuse lowers confidence. | Source attribution where mixed evidence is actually distinguishable. |
| G4 Heuristics | Existing authenticated LearningLadder used; ordinary verifiers cannot promote policy. | Evaluated candidate heuristic production. |
| G5 Procedural skills | Existing Skill TDD/promotion retained. | Conversion of repeated verified patterns through held-out evaluation. |
| H1 Scenarios | Existing EVAL domains and #546 divergence minimization. | Strategy-specific held-out orchestration suite. |
| H2 Cost | Typed strategy usage and existing evaluation metric seams. | Full bound attempt graph, resource and environment identity. |
| H3 Ablations | No measured critic/delegation/rotation lift claimed. | Independent controlled comparisons. |
| H4 Promotion | #546 kind-bound lifecycle gates; new results invalidate stale evidence; retained divergence must block promotion. | Production selfmod/router/strategy-policy consumers and independent score authority. |
| H5 Rollout | Strategy default is off; observation does not execute suggested actions. | Shadow/canary cohorts, evidence-gated promotion and rollback; no default-on claim. |

## Admission and recovery policy

- Root/parent budgets are authority, not metadata supplied by a model. Child
  reservations must be conserved across siblings and descendants. Active child
  work reserves its full ceiling; terminal work retains actual spent usage and
  can release unused allowance only. Concurrent wall time is additive
  worker-seconds, not the maximum sibling duration.
- A legacy unanchored continuation has no invented parent resource pool. Its
  compatibility behavior must not be cited as aggregate root-budget enforcement.
- Strategy observations are diagnostic only. Their separate checkpoint store has
  no effect-journal high-water coupling; an error class or stored recommendation
  is never evidence that replay is safe.
- Reviewer independence is scoped context and explicit routing. It is not proof
  of statistical independence or measured quality lift.
- Runtime signature, archive integrity, artifact readiness, evaluator score and
  promotion approval are distinct authorities; none substitutes for another.

## Verification record

- First combined defect/foundation run: 281 passed, 1 Windows-only skip.
- Broad Linux run during initial integration: 17,020 passed, 150 skipped,
  3 failed. Two failures were generated-document freshness; one was a newly
  introduced legacy error-string parser. These failures must be resolved before
  publication. Later focused changes require their own checks and exact-commit CI.
- Full Ruff was run and reported 10,897 diagnostics across the repository. This
  is not a clean full-tree lint result. Compare new diagnostics against the
  baseline; do not silently reclassify inherited lint debt as passing.
- PR #546 merged-main reconciliation: focused evaluation 51 passed; requirement
  evidence, documentation authority and history-privacy gates passed. Subsequent
  gate-staleness regressions have separate focused tests.
- Platform skips include Windows MIC/PowerShell/managed runtime, live PostgreSQL,
  unavailable provider/model services and optional dependencies. Synthetic tests
  do not replace those qualifications. Windows CI paths are concrete jobs.

## Completion gate

#510 remains open until its definition of done is evidenced: common recovery
semantics across Workbench/Autopilot/Fleet, restart-safe strategy/effect/child
state, materially different recovery, conserved recursive resources, measured
capability routing, attributable learning, strategy ablations and controlled
promotion/rollback. Obsolete recovery paths are removed only after equivalence
is established. This ledger deliberately keeps unfinished acceptance visible.
