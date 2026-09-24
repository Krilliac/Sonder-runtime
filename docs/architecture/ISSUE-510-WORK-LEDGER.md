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
| 6 Critical runtime guards | [Guard inventory](evidence/ISSUE-510-GUARD-INVENTORY-2026-09-23.md), #553 no-progress guards. | Shared opt-in host request admission covers physical Ollama/OpenAI chat and embedding sends. Top-tier lanes are durable per parent; cross-process request-rate and batching remain distinct gaps. |
| 7 Guard canaries | `tests/test_issue510_existing_guard_canaries.py`, verification/no-progress suites. | Existing canaries retained; concurrency, forged outcome, cancellation and signature refusal added. |
| 8 Evidence-based learning ladder | MEM-003, merged #518/#525 authenticated observations. | Strategy experience extends canonical memory UoW and LearningLadder; ordinary successful runs never authorize policy promotion. |
| 9 Skill TDD | SKILL-006 / MEM-008, existing skill promotion tests. | Procedure generation still requires held-out evaluation and promotion; no automatic conversion from a single strategy success. |
| 10 Serializable checkpoints | SESSION-004/005/007/009, LOOP-008, merged #518/#523. | Strategy uses RuntimeCheckpointRepository CAS and sealing. Observation DB is not an effect journal and cannot authorize replay. |
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
| A4 Trace | Existing sealed checkpoint port; immutable attempt IDs, CAS, restart, exact charges and nonexpanding budgets. Pending action reservations and project guards retain uncertain execution across reinvocation. Codegen/Autopilot observers are explicit opt-in. | Codegen active canary refuses on the current same-user build profile: candidate builds can access the seal key. A supported lower-privilege build adapter remains required; the observation store is not an effect journal. |
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
| E1 Conformance battery | Existing chat/structured/cancellation battery retained. | Broader protocol probes; unsupported/unmeasured cases remain unknown. |
| E2 Profile persistence | Persisted typed backend identity binds model digest/quantization, backend/version, tokenizer/template, context and hardware. | Host-measured native provider coverage; unsupported protocols remain unknown. |
| E3 Eligibility | Opt-in production routing refuses unknown/stale/failed/synthetic/mismatched evidence; exact concrete provider identity is checked before and after chat, role and embedding calls. | Live measured identities and wider protocol qualification. |
| E4 Escalation | Bounded codegen policy and existing logical route tiers. | Recent measured capability and separate role budgets. |
| E5 Attribution | Observers retain host-provided route/tier and available counters. Codegen records model/critic/build counts; Workbench and Autopilot currently record attempts only. | Exact resolved-model and full cost attribution; evaluation of whether escalation helped, including negative controls. |
| F1 ToolSearch | Implemented bounded deterministic summary search over an immutable, host-granted inventory. | Additional host surfaces beyond native MCP. |
| F2 Lazy schemas | `mcp --native --progressive-tools` exposes search/load tools, loads at most eight schemas and refuses calls before visibility. | Broader model request integration and resource evaluation. |
| F3 Skills | Existing ProgressiveSkillRegistry / LiveAgentContextProducer is summary-first and lazy. | Strategy-selected procedural skills still cross current policy/TDD gates. |
| F4 Schema replay identity | Loaded schema manifest binds inventory and selected schemas; typed request carries immutable selection; durable tool audit records it. | Native typed and compatibility calls share the existing durable audit, including schema loads/refusals and immutable call-begin selection; compatibility receipts explicitly retain their narrower authority. |
| F5 Cache telemetry | CTX-009, merged #531/#537/#545, selected schema identity in stable prefix. | Measure native progressive discovery's real prefix reuse. |
| G1 Experience | Content-free projection of sealed strategy attempts into canonical memory UoW. | Cross-host crash recovery integration. |
| G2 Failure retrieval | Scope/failure/family/language/verifier filters; bounded pre-attempt ContextPlanner projection with typed provenance and cross-project replay refusal. | Codegen and Autopilot select before local model calls using actual context plans and attribute recorded outcomes only after a response; held-out irrelevant-memory controls remain required. |
| G3 Attribution | Selected strategy-memory reference plus host-observed model response required for reuse credit; failed reuse lowers confidence. Transport refusal before a response leaves selection unpenalized. | Source attribution where mixed evidence is actually distinguishable. |
| G4 Heuristics | Existing authenticated LearningLadder used; ordinary verifiers cannot promote policy. | Evaluated candidate heuristic production. |
| G5 Procedural skills | Existing Skill TDD/promotion retained. | Conversion of repeated verified patterns through held-out evaluation. |
| H1 Scenarios | Thirteen deterministic synthetic policy canaries replay through sealed trace storage/readback and bind case input/source digests; #546 divergence minimization retained. | No independently owned evaluation case manifest or real-task held-out corpus is attached; synthetic canaries cannot authorize promotion. |
| H2 Cost | Strategy evaluation binds attempt graph, policy/role/tool/memory/skill/runtime identities and resource metrics. | Independent real-task receipt authority and measured live cost. |
| H3 Ablations | No measured critic/delegation/rotation lift claimed. | Independent controlled comparisons. |
| H4 Promotion | #546 kind-bound lifecycle gates; new results invalidate stale evidence; retained divergence blocks promotion. | Independent real-task score/receipt authority; synthetic agreement is not task success. |
| H5 Rollout | Default off; explicit observe/shadow/deterministic canary controls preserve host authority. Fleet pure-text canary can suppress retry; Codegen canary refuses without independently isolated build execution. | Build isolation, independent task grades and live promotion evidence remain required; no default-on claim. |

## Chat-lane convergence delta

The later [chat-lane request](https://github.com/Krilliac/Sonder-runtime/issues/510#issuecomment-5809305131)
remains part of #510. It is not implemented by the strategy recovery batch.

| Workstream | Existing foundation | Remaining implementation and acceptance |
|---|---|---|
| Explicit Chat policy | `application/chat/handle_chat.py` uses the canonical ModelGateway and session capture; `domain/runtime_policy/rules.py` already separates model tiers from execution lanes. | Add the `chat` lane and `chat -> general` policy mapping without a new tier, gateway or store. Preserve explicit model pins and single-model deployments; verify ordinary conversation stays in Chat and record its selected route. |
| Typed Chat-to-work handoff | Existing parent-bound agent-lane entrypoints enforce scope and authority; chat and worker lifecycle telemetry exist separately. | Connect a typed `ChatHandoff` preserving objective, constraints, project identity and bounded durable context references to eligible Workbench/Autopilot/Fleet requests. Verify refused/allowed handoffs, conversational tool restrictions, structured work return and durable attribution without copying the full transcript or widening permissions. |

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

The current build runner executes candidate code with the host user identity. A
local negative probe confirmed that such code can read the private strategy seal
key even with POSIX mode `0600`. Production active Codegen therefore refuses before
build/model dispatch until a supported lower-privilege execution adapter exists.

The separate, non-gating Windows diagnostic job exercises dummy protected-state
reads, inherited MIC labels and tampering, process handle access, a WMI broker,
staged compilation and a per-run restricting-SID experiment. Its pass/fail/skip
results are diagnostic evidence, not production isolation qualification. Ordinary
low MIC, file labels alone, a restricting SID or a successful staged build cannot
substitute for demonstrated process, desktop, credential and broker boundaries.
Task Scheduler/other brokers and a separate desktop remain explicit unqualified
surfaces; no adapter is enabled from these probes.

## Completion gate

#510 remains open until its definition of done is evidenced: common recovery
semantics across Workbench/Autopilot/Fleet, restart-safe strategy/effect/child
state, materially different recovery, conserved recursive resources, measured
capability routing, attributable learning, strategy ablations and controlled
promotion/rollback. Obsolete recovery paths are removed only after equivalence
is established. This ledger deliberately keeps unfinished acceptance visible.
