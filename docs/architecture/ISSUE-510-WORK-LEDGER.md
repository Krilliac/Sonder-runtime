# Issue 510 execution ledger

Updated 2026-09-24. This is an implementation and verification work ledger for
[#510](https://github.com/Krilliac/Sonder-runtime/issues/510), including the
2026-09-24 strategy/recovery program and the associated ten-item defect audit.
Original baseline: `a6a082859d4d3d4ec80cf6497bd37d6052bba5e9`.
Continuation baseline: `65f64a6ba4ce8e6ea22fcc335f10b803633c4454`; see the
[continuation qualification](evidence/ISSUE-510-CONTINUATION-2026-09-24.md).

The [master specification](SONDER-MASTER-IMPLEMENTATION-SPEC.md) and append-only
[requirement evidence](evidence/requirements.jsonl) remain the requirement/status
authorities. A local passing test, an observation adapter, or an entry below does
not make a complete requirement verified. No unchecked master item is checked by
this ledger. Existing implementations are extended instead of being duplicated.

## Defect audit

| Item | Implementation and meaningful verification | Qualification |
|---|---|---|
| W1 Artifact signature/publisher trust | Windows signable artifacts reject unavailable verification; exact normalized publisher organization matching; verifier environment stripped; non-Windows results explicitly disclose absent signature verification. `tests/test_artifact_fetch_tools.py`, `tests/test_runtime_artifact_adapters.py`. | Qualified on native Windows at `6dcd9924`: real OS-signed PowerShell PE and fixed Microsoft publisher pin passed, alongside refusal/tamper fixtures. |
| W2 Root/concurrency counting | Root anchor is excluded; transactional SQLite/PostgreSQL admission conserves nested resources and owner-wide active slots across operation roots. Reused reservations count once. | SQLite multi-process races pass; native owned PostgreSQL primary/standby qualification passed 21 tests without skips at `6dcd9924`. |
| W3 Cancellation and reserved workers | Transactional cancellation fence prevents late success; cancelled ancestors prevent new descendants; pre-start cancellation terminalizes the record; resume clears stale verification. Provider launch failure releases only a newly created unstarted row with its expected revision. | Implemented; race and reused-reservation canaries pass. |
| W4 Wall budget | Child deadline and measured elapsed time reject success after budget exhaustion. | Implemented; deterministic late-success test. |
| W5 Durable outcome authority | Delegation verification compares the full canonical terminal result, including status, output, error and usage. Identical receipts are idempotent; conflicting receipts fail. | Implemented; forged-success and resumed-result regressions pass. |
| W6 Compiler-feedback repair | Next codegen candidate receives bounded prior source, normalized diagnostics and progress; explicit multi-route policy admits a scoped critic then a distinct model; incomplete evidence and critic failure degrade safely; best candidate retained. | Implemented and unit/integration tested; live model benefit remains unmeasured. |
| W7 Runtime closure / #550 | Inventory and byte/count bounds precede hashing; scan/hash replacement detected; full closure still hashed. Canonical Windows installer can provision a separate pinned `venv-managed`; host selects and validates its bound profile. | Linux contract tests and native Windows installer/profile/real launch qualification pass. Existing size/count bounds remain; no arbitrary new production time cap. |
| W8 Release dependency gate | Tagged release depends on integrity, Flutter analysis, reusable exact-tag Python CI and installer-owned Windows profile/launch qualification. Native MIC checks run independently after preceding test failures. Existing required `tests` context retained. | Workflow structure and local release tests pass; tagged publication itself is not exercised. |
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
| 3 Productive parallelism | `domain/adaptive_concurrency.py`, ownership scheduler from #552; AGENT-007. | SQLite writer transaction / PostgreSQL row lock conserve aggregate resource reservations; active speculative lane/hypothesis identities are unique per task scope. Owner-wide concurrency spans durable roots. Native PostgreSQL primary/synchronous standby qualification passed 21 tests without skips at `6dcd9924`; see the verification record. |
| 4 Worker execution contracts | AGENT-009 / #541, `application/agents/delegation_service.py`. | Canonical result binding, scoped critic context and inherited budgets. |
| 5 Deterministic workflow gates | Delegation role workflow, artifact readiness barrier, EVAL proposal lifecycle. | PR #546 merged after reconciliation and green CI; exact artifact identity and canonical child results bind readiness fan-in. |
| 6 Critical runtime guards | [Guard inventory](evidence/ISSUE-510-GUARD-INVENTORY-2026-09-23.md), #553 no-progress guards. | Shared opt-in host request admission covers physical Ollama/OpenAI chat and embedding sends. Top-tier lanes are durable per parent. Opt-in physical request limits now share a durable bucket across processes and restart, with cold-start and missing-state refusal tests; batching/coalescing remains separate work. |
| 7 Guard canaries | `tests/test_issue510_existing_guard_canaries.py`, verification/no-progress suites. | Existing canaries retained; concurrency, forged outcome, cancellation and signature refusal added. |
| 8 Evidence-based learning ladder | MEM-003, merged #518/#525 authenticated observations. | Actual authenticated managed dispatcher receipts now exercise repeated-principal non-promotion, independent-principal fact promotion, verified failure demotion, and reopen. Strategy experience extends the canonical UoW; ordinary success never authorizes policy promotion. |
| 9 Skill TDD | SKILL-006 / MEM-008, existing skill promotion tests. | Procedure generation still requires held-out evaluation and promotion; no automatic conversion from a single strategy success. |
| 10 Serializable checkpoints | SESSION-004/005/007/009, LOOP-008, merged #518/#523. | Strategy uses RuntimeCheckpointRepository CAS and sealing. Real typed file mutations now declare journal effects, and gateway subprocess crash cuts fence uncertain execution. Child-session checkpoint/high-water coupling remains open; observation DB cannot authorize replay. |
| 11 Hybrid memory | MEM-004/005, merged #538; authoritative facts and scoped indexes. | Strategy memory carries provenance, attribution, failed-reuse decay and bounded context references. |
| 12 Isolated harness evolution | SELFMOD-002 / #517/#519, `scripts/selfmod_low_integrity.py`. | Unsupported-host auto execution refuses. Parent-side filtered tests are not an independent oracle: candidate code can forge the child result frame. Automatic approval remains blocked; Low MIC does not establish confidentiality or network isolation. |

## Strategy/recovery program

The series IDs below are from issue comment
[5808455090](https://github.com/Krilliac/Sonder-runtime/issues/510#issuecomment-5808455090).
“Implemented” identifies a concrete mechanism, not full rollout acceptance.

| Series | Implementation / status | Remaining acceptance |
|---|---|---|
| A1 Contracts | Implemented in `domain/strategy/models.py`: bounded typed attempts, signatures, references, resource usage and budgets. | Integrate every host producer; no raw payload duplication. |
| A2 Failure vocabulary | Typed source-independent failure classes and conservative host safety properties. | Every host's source-specific projection must retain its evidence. |
| A3 Progress | Typed comparable vectors; incomplete/scope-mismatched evidence is incomparable; regressions take precedence. | General research/debugging projections and measured accuracy. |
| A4 Trace | Existing sealed checkpoint port; immutable attempt IDs, CAS, restart, exact charges and nonexpanding budgets. Pending action reservations and project guards retain uncertain execution across reinvocation. Codegen/Autopilot observers are explicit opt-in. | Codegen canary requires an explicit Linux container adapter with exact host source grants and a pinned local image. Default/Windows execution still refuses. Native container CI qualification is pending; observation storage is not an effect journal. |
| B1 Feedback repair | Codegen production loop passes actual previous candidate and compiler diagnostics. | Live compiler/model quality evaluation. |
| B2 Scoped critic | Critic receives source/task/verifier facts and constraints, excluding implementer rationale. | Measure marginal critic benefit with held-out ablations. |
| B3 Model rotation | Explicit route policy; independent resolved model; bounded escalation following repeated comparable failure. | Measured capability eligibility and role-resource qualification. |
| B4 Best candidate | Improving/verified candidate retained; no blind overwrite on later regression. | Cross-host candidate restoration. |
| C1 Controller | Pure deterministic bounded decisions; effect uncertainty and host policy precede replay/repair. Success reaches the host completion gate even on the final budgeted attempt. | Authority remains with existing host gates; promoted active recovery needs equivalence evidence. |
| C2 Autopilot | Production observe adapter, durable backfill and pre-task bounded memory retrieval; response receipts gate reuse attribution. | Active shared-policy migration and fault matrix. |
| C3 Workbench | Production terminal observer, durable backfill and explicit rollout controls use existing agent/effect/context boundaries. | Active recovery cannot bypass explicit resume authority or effect reconciliation. |
| C4 Fleet | Production per-attempt observer, shadow comparison and deterministic pure-text canary may suppress legacy retries. Repository workers remain observe-only. | Controlled live quality/equivalence evaluation and wider safe action migration. |
| D1 Structured results | Existing AGENT-009 plus exact durable terminal result binding and typed specialist/delegation proposals. | Cross-host live qualification. |
| D2 Reservations | Atomic hierarchical step/token/worker-time reservations, depth/child limits, owner-wide concurrency and ownership admission; unknown token usage retains its complete reservation. | Native PostgreSQL primary/synchronous standby qualification passed 21 tests without skips at `6dcd9924`, including the corrected registry lookup, duplicate-key admission and fixture budget. Zero-test/skip-only runs are rejected. |
| D3 Specialist proposals | Canonical delegation service accepts bounded typed specialist proposals under an owner-bound operation root and inherited finite budgets. | Native platform and live-model quality qualification. |
| D4 Hypotheses | Canonical transactional admission rejects duplicate active speculative lanes or hypothesis digests within the root task scope. | Live search diversity and marginal-benefit evaluation. |
| D5 Evidence synthesis | Fan-in rechecks canonical child outputs, artifact ID/size/source specification and host verifier metrics; recursive proposals retain scoped evidence. | Independent verifier authority and complete live exhausted-search qualification. |
| E1 Conformance battery | Bounded OpenAI-compatible chat/structured diagnostics verify response shape and provider model labels. | CLI evidence stays synthetic: operator identity files cannot attest provider artifacts. Runtime tool protocols and in-flight cancellation stay unknown. |
| E2 Profile persistence | Persisted typed backend identity binds model digest/quantization, backend/version, tokenizer/template, context and hardware. | Host-measured native provider coverage; unsupported protocols remain unknown. |
| E3 Eligibility | Opt-in production routing refuses unknown/stale/failed/synthetic/mismatched evidence; configured identity checks surround chat, role and embedding calls. Arbitrary callbacks and the current CLI cannot mint nonsynthetic evidence. | A concrete provider/host attestation path must bind measured identity before live eligibility can be claimed. |
| E4 Escalation | Bounded codegen policy and existing logical route tiers. | Recent measured capability and separate role budgets. |
| E5 Attribution | Observers retain host-provided route/tier and available counters. Codegen records model/critic/build counts; Workbench and Autopilot currently record attempts only. | Exact resolved-model and full cost attribution; evaluation of whether escalation helped, including negative controls. |
| F1 ToolSearch | Implemented bounded deterministic summary search over an immutable, host-granted inventory. | Additional host surfaces beyond native MCP. |
| F2 Lazy schemas | `mcp --native --progressive-tools` exposes search/load tools, loads at most eight schemas and refuses calls before visibility. | Broader model request integration and resource evaluation. |
| F3 Skills | Existing ProgressiveSkillRegistry / LiveAgentContextProducer is summary-first and lazy. | Strategy-selected procedural skills still cross current policy/TDD gates. |
| F4 Schema replay identity | Loaded schema manifest binds inventory and selected schemas; typed request carries immutable selection; durable tool audit records it. | Native typed and compatibility calls share the existing durable audit, including schema loads/refusals and immutable call-begin selection; compatibility receipts explicitly retain their narrower authority. |
| F5 Cache telemetry | CTX-009, merged #531/#537/#545, selected schema identity in stable prefix; actual persisted agent request replay is qualified in a fresh interpreter after rules/skills change. | Measure native progressive discovery's real prefix reuse; doubles do not prove live provider cache performance. |
| G1 Experience | Content-free projection of sealed strategy attempts into canonical memory UoW. | Cross-host crash recovery integration. |
| G2 Failure retrieval | Scope/failure/family/language/verifier filters; bounded pre-attempt ContextPlanner projection with typed provenance and cross-project replay refusal. | Codegen and Autopilot select before local model calls using actual context plans and attribute recorded outcomes only after a response; same-project irrelevant task-family controls now pass through actual pre-model selection. Independent task-quality ablations remain required. |
| G3 Attribution | Selected strategy-memory reference plus host-observed model response required for reuse credit; failed reuse lowers confidence. Transport refusal before a response leaves selection unpenalized. | Source attribution where mixed evidence is actually distinguishable. |
| G4 Heuristics | Existing authenticated LearningLadder used; ordinary verifiers cannot promote policy. | Evaluated candidate heuristic production. |
| G5 Procedural skills | Existing Skill TDD/promotion retained. | Conversion of repeated verified patterns through held-out evaluation. |
| H1 Scenarios | Thirteen deterministic synthetic policy canaries replay through sealed trace storage/readback and bind case input/source digests; #546 divergence minimization retained. | No independently owned evaluation case manifest or real-task held-out corpus is attached; synthetic canaries cannot authorize promotion. |
| H2 Cost | Strategy evaluation binds attempt graph, policy/role/tool/memory/skill/runtime identities and resource metrics. | Independent real-task receipt authority and measured live cost. |
| H3 Ablations | No measured critic/delegation/rotation lift claimed. | Independent controlled comparisons. |
| H4 Promotion | #546 kind-bound lifecycle gates; new results invalidate stale evidence; retained divergence blocks promotion. | Independent real-task score/receipt authority; synthetic agreement is not task success. |
| H5 Rollout | Default off; observe/shadow/deterministic canary controls preserve host authority. Fleet pure-text canary can suppress retry; Codegen requires the opt-in scoped Linux container build adapter. | Non-skipped native containment probes, independent task grades and live promotion evidence remain required; no default-on claim. |

## Chat-lane convergence delta

The later [chat-lane request](https://github.com/Krilliac/Sonder-runtime/issues/510#issuecomment-5809305131)
remains part of #510 and is being integrated in this continuation.

| Workstream | Existing foundation | Remaining implementation and acceptance |
|---|---|---|
| Explicit Chat policy | The `chat` lane maps to general by default through the existing policy and ModelGateway. Typed/HTTP paths retain operator strict aliases and explicit tier/model pins across mixed provider bindings; session capture/replay records lane, reason and selected route. | Focused composed and real HTTP regressions pass; integrated hosted checks remain required. No additional model tier or store. |
| Typed Chat-to-work handoff | Owner-scoped HTTP admission retains typed objective/mode/project/provenance and a verified prior model-response reference; canonical admission/return events and JSON/SSE receipts survive restart. One classification and existing authorization/idempotency gates remain. | `returned` is a handler outcome, not task success. The source reference is provenance only; legacy lanes still need authorized prior-context consumption and separately enforced constraints/success criteria. |

Generic agent-lane tests do not establish these Chat-specific acceptance criteria.

## Admission and recovery policy

- Root/parent budgets are authority, not metadata supplied by a model. Child
  reservations must be conserved across siblings and descendants. Active child
  work reserves its full ceiling. Terminal work retains measured steps/time;
  unknown output-token usage retains the complete reservation. Only proven unused
  allowance can be released. Concurrent wall time is additive
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

- PR #554 merged as `5b4951e8891ad2fde0615cf59ce14ab6bf583033` after
  exact published head `1254e5d3149c13cced9c4191f041a73697ddbf10` passed
  Linux CI, Windows-focused tests, installer-owned runtime qualification,
  PostgreSQL conformance and all application builds. The two review threads
  were addressed and resolved. Issues #550, #551 and #514 are closed against
  their scoped acceptance; broader master requirements remain unchanged.
- Continuation focused tests qualify authenticated multi-principal learning,
  actual typed file-write crash refusal, local compute launch reconciliation,
  fresh-process context replay, shared rate admission, composed chat routing,
  durable HTTP handoff receipts and the real Codegen composition seam. Native
  Docker and exact integrated hosted checks gate platform qualification.
  The lint comparison has zero new groups (10,819 baseline / 10,814 current),
  retaining inherited diagnostics after mapping the moved isolated runner.
- The final continuation code at `32cabd3b` (identical published tree at
  `7fe42da7102f83fa6c9f0a8e489fa75ee16c0913`) passed the full Linux suite:
  **17,441 passed, 169 skipped**, 3 warnings and 4 subtests passed in 344.35
  seconds. Architecture, append-only requirement evidence, error signals,
  history privacy, documentation links and documentation authority gates pass.
  Earlier integration failures led to corrected chat expectations, owned
  container readers and bounded partial-admission cleanup. A separate acceptance
  regression fixed truncated skill metadata claiming a complete live prefix.
  Native Windows and Docker results must be assessed at the final PR head;
  this local run does not replace them. The Docker skips are explicit.
- First combined defect/foundation run: 281 passed, 1 Windows-only skip.
- Initial broad Linux run: 17,020 passed, 150 skipped, 3 failed. Generated
  documentation and the introduced error parser were subsequently corrected.
- Second broad Linux run: 17,148 passed, 153 skipped, 2 failed. The installer
  documentation and earlier budget-refusal expectations were corrected; later
  focused checks pass. This is not yet a passing full-suite result at final head.
- Third broad Linux run at `5f2a3f22`: 17,255 passed, 157 skipped, 1 obsolete
  release-gate expectation failed; corrected in `f6f21879`. Its focused suite
  subsequently passed (77 passed, 1 skipped). Later integrations still need CI.
- Integrated recursive admission/memory/native discovery/evaluation selection:
  78 passed. Further integrations require exact-revision validation.
- Full Ruff initially reported 10,897 diagnostics. Baseline comparison is repeated after integration; inherited lint debt
  remains. Diagnostic line-number mentions are normalized for comparison; this
  is not a full-tree lint pass.
- PR #546 merged after required CI and application builds passed. Promotion
  evidence staleness and retained-divergence regressions have dedicated tests.
- PR #554 revision `94c1a7119ce385cd6d5777e8e8d0bf37c3513864`:
  installer-owned Windows profile and real launch: 19 passed; Windows
  owner/artifact/lifecycle suite: 193 passed, 6 skipped; release smoke: 1 passed.
  Native MIC initially had one stale mocked attestation fixture. After its
  correction, revision `9c2bb9a7` passed 20 MIC tests (1 Git Bash test intentionally
  deselected), 193 Windows owner/artifact/lifecycle tests and release smoke. Artifact
  signature fixtures do not establish a real signed-PE positive verification.
- Exact published code head `6dcd99246a1a8dca5ae6aaff63bb94c7f2180a9d`: native
  PostgreSQL primary/synchronous standby passed 21 tests with zero skips; Windows
  owner/artifact/lifecycle plus real fixed-publisher Authenticode passed 194 tests
  (6 platform skips), release smoke passed 1, and MIC passed 20 (1 Git Bash
  deselection). Installer-owned Windows runtime/profile/launch also passed.
  [PostgreSQL job](https://github.com/Krilliac/Sonder-runtime/actions/runs/35973735098/jobs/107549187387),
  [Windows job](https://github.com/Krilliac/Sonder-runtime/actions/runs/35973735104/jobs/107549187540).
- Fourth broad Linux run: 17,288 passed, 159 skipped, 2 failed. The canonical
  model-error formatting path and the exact competing prepared-claim test
  expectation are corrected, with focused regressions passing.
- Final combined Linux run after integration: **17,290 passed, 165 skipped**,
  3 warnings and 4 subtests passed (222.97 seconds). Windows-only isolation
  diagnostics are among the explicit Linux skips. Architecture, evidence,
  documentation and error-signal gates pass; the lint comparison has zero new
  diagnostic groups (10,889 baseline / 10,819 current), not a full-tree lint pass.
- Provider/model quality and independent task/held-out grading remain distinct
  qualifications. Synthetic controller canaries do not replace them.
- History privacy ratchet passed with zero new/unexpected findings and seven
  existing known findings; this is not a claim of a debt-free history.
- Recovery-session validation on 2026-09-24 reproduced **17,290 passed,
  165 skipped**, 3 warnings and 4 subtests passed (324.58 seconds, Python 3.12,
  four xdist workers). The two previously failing CI test files passed all
  25 tests. Architecture, append-only evidence/base-diff, error-signal,
  documentation and history-privacy gates passed; offline smoke replay passed
  4/4 cases and tool-policy evaluation passed 33/33. The recovered file delta
  was applied on top of published `6dcd9924`, preserving the existing PR
  history. Hosted checks for this new recovery commit are still required.

## Active-build isolation boundary

The legacy build runner executes candidate code with the host user identity. A
local negative probe confirmed that such code can read the private strategy seal
key even with POSIX mode `0600`. Active Codegen now requires the opt-in
[Linux container adapter](../security/CODEGEN-CONTAINER-BUILD.md), a pinned local
image and exact host-granted source inputs. Default and Windows profiles refuse
before build/model dispatch. Native Docker containment requires the dedicated
workflow to pass all four probes without skips at the reviewed PR head; local
adapter tests or skipped probes cannot qualify that boundary.

The separate, non-gating Windows diagnostic job exercises dummy protected-state
reads, inherited MIC labels and tampering, process handle access, a WMI broker,
staged compilation and a per-run restricting-SID experiment. Its pass/fail/skip
results are diagnostic evidence, not production isolation qualification. Ordinary
low MIC, file labels alone, a restricting SID or a successful staged build cannot
substitute for demonstrated process, desktop, credential and broker boundaries.
Task Scheduler/other brokers and a separate desktop remain explicit unqualified
surfaces; no adapter is enabled from these probes.

## Completion gate

### 2026-09-24 stalled-chat recovery and added hardening workload

The continuation resumes PR #555 from `ce0291bf`, preserving its published
history. The final effect-receipt review fix described in the stalled chat was
not present on that branch and is being recovered with fresh verification.
The operator added bounded lock waits, holder diagnostics, progress-aware
fleet stalls, process-identity checks, migration deadlines, a verified selfmod
pidfile, and personal-alias recovery diagnostics. The
[site-by-site workload and incident evidence](evidence/STALL-RECOVERY-2026-09-24.md)
separate observed publication interruption from the unavailable backend cause.
Completion requires the integrated suite and exact-head hosted checks; prior
chat-reported tests do not qualify reconstructed code.

The reconstructed working tree passed the complete Linux suite: 17,503 passed,
167 skipped, three warnings and four subtests (288.88 seconds), plus the six
repository gates and golden lanes (4/4 smoke, 33/33 policy). The expanded native
Windows focused cohort passed 533 tests with 12 explicit skips; account-control
passed 43 tests and recovery HTTP passed both tests after config-snapshot and
SQLite sidecar-race fixes. DATA-003–007 and
OPS-006 gained bounded real-file/process qualification without promotion of
the broader master requirements. Exact-head hosted evidence is still pending.

#510 remains open until its definition of done is evidenced: common recovery
semantics across Workbench/Autopilot/Fleet, restart-safe strategy/effect/child
state, materially different recovery, conserved recursive resources, measured
capability routing, attributable learning, strategy ablations and controlled
promotion/rollback. Obsolete recovery paths are removed only after equivalence
is established. This ledger deliberately keeps unfinished acceptance visible.
