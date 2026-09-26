# Issue 510 execution ledger

Updated 2026-09-25. This is an implementation and verification work ledger for
[#510](https://github.com/Krilliac/Sonder-runtime/issues/510), including the
2026-09-24 strategy/recovery program, the associated ten-item defect audit, and the 2026-09-25 cross-provider agent/connector delta.
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
| 6 Critical runtime guards | [Guard inventory](evidence/ISSUE-510-GUARD-INVENTORY-2026-09-23.md), #553 no-progress guards. | Shared opt-in host request admission covers physical Ollama/OpenAI chat and embedding sends. Top-tier lanes are durable per parent. Opt-in physical request limits now share a durable bucket across processes and restart, with cold-start and missing-state refusal tests. Batching/coalescing (2026-09-26): the agent loop steers runs of distinct single `file_read` calls to the registered read-only `context_pack` (typed advisory at 3, typed non-dispatching refusal after 6, run ends at the third refusal in one window; refusal count, retries of failed reads and pack-returned files are window-scoped exemptions; `context_pack` model view fitted per file within the 6000-character observation budget; thresholds capped at 19 by the 20-step clamp; bounded typed config; `agent_guard` telemetry; canaries and negative controls in `tests/test_batch_coalescing_guard.py`). Mutating/execution tools are never steered; other read families have no registered batch form, and selfmod/autopilot runs with argument-aware policies plus the typed/native gateway are not covered. |
| 7 Guard canaries | `tests/test_issue510_existing_guard_canaries.py`, verification/no-progress suites. | Existing canaries retained; concurrency, forged outcome, cancellation and signature refusal added. |
| 8 Evidence-based learning ladder | MEM-003, merged #518/#525 authenticated observations. | Actual authenticated managed dispatcher receipts now exercise repeated-principal non-promotion, independent-principal fact promotion, verified failure demotion, and reopen. Strategy experience extends the canonical UoW; ordinary success never authorizes policy promotion. |
| 9 Skill TDD | SKILL-006 / MEM-008, existing skill promotion tests. | Procedure generation still requires held-out evaluation and promotion; no automatic conversion from a single strategy success. |
| 10 Serializable checkpoints | SESSION-004/005/007/009, LOOP-008, merged #518/#523. | Strategy uses RuntimeCheckpointRepository CAS and sealing. Real typed file mutations now declare journal effects, and gateway subprocess crash cuts fence uncertain execution. Child-session checkpoint/high-water coupling remains open; observation DB cannot authorize replay. A journaled child runner's typed gateway calls now use deterministic journal identities (child run, worker, dispatch attempt, checkpoint-recorded call ordinal, request digest): a resumed re-issue meets its settled receipt and a divergent one is refused, and any other failed admission halts the runner's calls ([#515 doc](REMAINING-AGENT-515-EFFECT-JOURNAL.md)). No production child runner issues typed gateway calls yet: the composed conversational runner only calls the model gateway, so this identity is proven only with a test-substituted runner. |
| 11 Hybrid memory | MEM-004/005, merged #538; authoritative facts and scoped indexes. | Strategy memory carries provenance, attribution, failed-reuse decay and bounded context references. |
| 12 Isolated harness evolution | SELFMOD-002 / #517/#519, `scripts/selfmod_low_integrity.py`, `scripts/selfmod_linux_isolation.py`, `scripts/selfmod_oracle.py`, [#517 Linux isolation](REMAINING-SELFMOD-517-LINUX-ISOLATION.md); operator `/selfmod run` (REPL, HTTP, MCP) via `selfmod.operator_candidate_isolation` and the selfmod stage journal (`tests/test_selfmod_operator_isolation.py`). | Unsupported-host auto execution refuses; operator runs on such a host refuse unless an attended console operator passes `--unisolated`; operator stages hold the run lease, and pre-write legacy deploy/rollback refusals settle `:not-applied` and are retryable. Parent-side filtered tests are not an independent oracle, because candidate code can forge their frames. An evaluator-held oracle now grades the candidate. The expected values are kept in `0600` files that the kernel refuses to the Linux candidate uid, and this is proven for each run. The candidate receives nonce-bound inputs and emits raw outputs, and the parent compares them. A receipt binds the verdict to the tested and baseline digests. Its digest is a corruption check, so integrity rests on the ledger being closed to the candidate uid. The ledger stores only digests of the held inputs and outputs. Host auto-approval requires that receipt plus a fixed floor of passed gates (regression partitions, held-out, host grade), and nightly still stops for a human. Low MIC gives no confidentiality, so on Windows the oracle is never independent. The Linux uid supervisor now runs candidates in a supervisor-confirmed network namespace with no configured interface, under `no_new_privs`, and under a seccomp filter that allows only namespace-scoped socket families (which closes `AF_VSOCK`). A `linux-uid` attestation without all three is refused. The root-only canaries run under sudo in the `linux-selfmod-isolation` CI job, which has not yet run on a hosted runner. No general seccomp syscall allow-list is applied. |

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
| E5 Attribution | Observers retain host-provided route/tier and available counters. Codegen records model/critic/build counts. Workbench charges each attempt the lane's durable model turns (`used_steps`) not already charged to earlier sealed attempts, so a lane's total `model_calls` equals its counter (the trace refuses, inside its generation CAS, a charge derived from a history that has since changed, and the observer re-derives it); turns from unobserved attempts, including legacy attempts sealed before attribution in a lane that is still attributed, land on the next observed one. Autopilot records `tool_calls` as the count of distinct host tools in the receipt (a lower bound on calls), `verifier_calls` from `validation_attempted`, and a `validation_passed` progress metric for validate tasks; an interrupted (`uncertain`) Autopilot attempt is charged one attempt and no receipt counters, because the task still carries the previous attempt's receipt. Sealed attempts keep their original usage and progress on replay, and budgets only narrow to what a run was sealed with. A run sealed before attribution whose default budget cannot hold the attributed request (a Workbench lane with `max_steps` above 12, every Autopilot run) keeps charging attempts only, so it is not billed lifetime counters for already-observed legacy attempts and its shadow `match` does not flip on budget; a legacy Workbench lane with `max_steps` of 12 or less is charged the lane total on its next attempt, which `max_steps` still bounds (`tests/test_strategy_workbench_autopilot_attribution.py`). | Exact resolved-model attribution (the resolved model lives only in session `model.completed` facts, not on the lane or Autopilot run), token and wall cost for Workbench/Autopilot, exact Autopilot tool-call and model-call counts, and evaluation of whether escalation helped, including negative controls. |
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

## Cross-provider agent and connector delta

The 2026-09-25 cross-provider design extends the existing SEAM-009
`SubagentProvider`, durable worker registry, AGENT-009 structured delegation,
AGENT-010 independently routed roles, and MODEL-004/005/007/008/009 routing
requirements. It is a convergence slice, not a second orchestration stack.
The invariant is:

> Models may propose what should happen next; Sonder remains the authority that
> decides what is admitted, which provider executes it, what context leaves the
> host, what budget is reserved, what effects are allowed, and whether the
> result is accepted.

Provider identity is routing policy, not authority. The preferred deployment may
use Astra/OpenAI as the orchestrator and Claude as a specialist/reviewer, but
that preference must remain runtime policy rather than a hard-coded hierarchy.

### Control and communication contract

- External agents never receive another provider's credential and do not directly
  invoke one another. Logical peer consultation is physically
  `worker -> Sonder -> worker`.
- Child models may return typed handoff, consultation, specialist or descendant
  proposals. The canonical host admission path validates authority, inherited
  budgets, workspace ownership, provider capability, cloud/egress consent,
  credential availability, context policy and duplicate/replay state before
  dispatch.
- Use the existing `WorkerExecutionContract`, `WorkerContextPolicy`,
  `DelegationService`, worker registry, adaptive concurrency and artifact
  readiness barrier. Do not introduce a provider-specific worker store, retry
  engine, context archive, checkpoint database or permission system.
- Reviewer/critic calls default to scoped context: task/spec, accepted
  constraints, relevant source/diff/artifacts and verifier evidence. Do not
  automatically inherit implementer rationale, confidence, irrelevant chat
  history or previous critic conclusions.
- Mutating external workers use isolated worktrees/workspaces and explicit
  `owned_files`/`task_scope`. Provider success does not imply integration;
  deterministic verification and the existing integration/acceptance authority
  remain separate.
- A provider transport failure never silently invokes a second provider. A
  provider/model switch is a new durable Strategy Controller decision with its
  own route, budget and reason.
- If an external run may have started and Sonder loses the completion receipt,
  restart recovery must reconcile the provider-native run/session identity when
  possible. Otherwise classify the worker `RECOVERY_REQUIRED`; never blindly
  redispatch a potentially mutating task.

### Connector classes

Keep two execution surfaces distinct:

1. **ModelGateway connector** — Sonder owns the loop, tool exposure, context,
   permissions and continuation around direct model inference.
2. **Managed-agent SubagentProvider connector** — the external service owns a
   bounded multi-step agent loop, while Sonder owns admission, workspace,
   budgets, context/credential release, reconciliation and result acceptance.

One vendor may expose both surfaces and should therefore have two adapters rather
than one ambiguous connector.

### Connector target matrix

| Service / surface | Intended Sonder seam | Status / transport requirement | Important constraints |
|---|---|---|---|
| OpenAI Responses / direct models | `ModelGateway` | First-class target through the existing OpenAI-compatible/provider-dispatch path, with native adapter when required for provider-specific features. | Preserve exact model/tier pins, route receipts and no implicit provider failover. |
| OpenAI Agents API / Codex SDK | managed-agent `SubagentProvider` | First-class managed-agent target. Agents API exposes managed Codex harness sessions; Codex SDK is the self-hosted harness option. | Treat provider session/sandbox IDs as opaque durable external identities; Sonder still owns root budget and acceptance. Source: https://developers.openai.com/api/docs/guides/agents |
| Anthropic Messages API | `ModelGateway` | First-class direct-model target. | Tool loop may remain Sonder-owned; credential/egress and prompt/context release stay under existing policy. |
| Claude Managed Agents / Claude Agent SDK | managed-agent `SubagentProvider` | First-class managed-agent target. Managed Agents provide stateful hosted agents; Agent SDK exposes the Claude Code harness for custom integrations. | Scoped reviewer context, explicit workspace grants, no direct child-provider invocation. Sources: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/claude-api-skill and https://www.anthropic.com/news/enabling-claude-code-to-work-more-autonomously |
| xAI Grok REST/gRPC API | `ModelGateway` | First-class direct-model target. xAI exposes OpenAI-compatible REST plus gRPC and tool/function calling. | Prefer a dedicated conformance profile rather than assuming all OpenAI-compatible semantics. Preserve xAI cache/conversation identity only as provider metadata, not Sonder authority. Sources: https://docs.x.ai/developers/rest-api-reference/inference and https://docs.x.ai/developers/grpc-api-reference |
| Grok Bot | managed-agent `SubagentProvider` bridge | **Planned/experimental until xAI publishes a stable programmatic Bot-control surface suitable for host orchestration.** Current official docs center on persistent cloud-computer Bots controlled through Grok Bot clients, conversations, routines and connectors. | Do not automate the desktop/web UI as a production connector. If a supported API/SDK appears, bind each Bot as an opaque managed child. Provider-internal Bot-to-Bot chats/group orchestration must not bypass Sonder budgets/authority. Sources: https://docs.x.ai/grok-bot/overview and https://docs.x.ai/grok-bot/bots |
| Google Gemini direct API | `ModelGateway` | First-class direct-model target. | Conformance-gate tools, structured output, multimodal behavior and cancellation before capability advertisement. |
| Google Gemini managed Agents / Antigravity | managed-agent `SubagentProvider` | First-class managed-agent target. Google documents managed agents with hosted Linux sandboxes and an Antigravity agent. | Treat the Google sandbox as the provider execution world; reconcile artifacts/results into Sonder rather than granting it integration authority. Source: https://ai.google.dev/gemini-api/docs/agents |
| GitHub Copilot SDK / CLI | managed-agent `SubagentProvider` | First-class local/managed-agent target. GitHub documents a Copilot SDK over CLI/JSON-RPC with custom agents, MCP, lifecycle hooks and session management. | Run inside an isolated workspace; retain normal Git/tool permissions; provider-generated commits/PRs are artifacts awaiting Sonder verification. Source: https://docs.github.com/en/copilot/responsible-use/agents |
| GitHub Copilot cloud agent | managed-agent `SubagentProvider` | First-class repository-oriented target where account/repository policy permits. GitHub exposes issue assignment through REST/GraphQL and session/PR tracking surfaces. | Treat branch/PR/session identity as the external durable result; never equate PR creation with task success. Sources: https://docs.github.com/en/enterprise-cloud@latest/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api and https://docs.github.com/en/copilot/how-tos/copilot-on-github/use-copilot-agents/manage-and-track-agents |
| Cursor Agent CLI / ACP | managed-agent `SubagentProvider` | First-class local agent target. Cursor Agent supports headless automation and ACP over stdio/JSON-RPC. | Prefer ACP for structured lifecycle/control when sufficient; otherwise wrap headless CLI behind the same durable adapter and parser bounds. Source: https://prod.cursor.com/docs/cli/using |
| Mistral direct API | `ModelGateway` | First-class model target. | Conformance-gate structured output/tool use rather than inheriting assumptions from another provider. |
| Mistral Agents / Conversations | managed-agent `SubagentProvider` | First-class managed-agent target. Agents support persistent conversations, built-in/custom tools, connectors and handoffs. | Sonder remains the outer authority; provider-native handoffs are disabled or treated as one opaque child unless their descendant/resource behavior can be bounded and evidenced. Source: https://docs.mistral.ai/studio/agents/agents-api |
| DeepSeek API / Harness-compatible surfaces | `ModelGateway` first; managed-agent only after harness conformance | Direct model target through its OpenAI/Anthropic-compatible APIs; future harness integration is separately qualified. | Compatibility claims are insufficient for agent eligibility; pass the same cancellation/tool/result/identity battery. Source: https://api-docs.deepseek.com/guides/harness |
| OpenRouter | `ModelGateway` aggregator, optional agent SDK | Supported only behind an explicit aggregator profile. OpenRouter standardizes many providers/models and offers an agent SDK. | **Disable or fully surface automatic provider fallback/routing** when exact provider identity matters. Hidden fallback conflicts with Sonder's explicit SWITCH_MODEL/SWITCH_PROVIDER and replay/attribution contracts. Sources: https://openrouter.ai/developers and https://openrouter.ai/blog/tutorials/build-tool-calling-agent-loop/ |
| Generic OpenAI-compatible providers (for example Groq, Together, Fireworks, Cerebras and similar services) | `ModelGateway` | Connector family, not automatic eligibility. | Each endpoint/model must pass provider identity, tool-call, structured-output, timeout/cancellation, context and usage conformance before routing can advertise those capabilities. |
| Other coding-agent products without a stable documented headless/API/ACP/MCP control surface | bridge candidate only | Discovery/research target, not production support. | Do not ship GUI-driving adapters merely to claim coverage. Promote only after a stable programmable contract and lifecycle/recovery semantics are documented and tested. |

The matrix is intentionally capability-based. Adding a provider to configuration
does not make it eligible for orchestration.

### Required provider profile

Extend the measured provider/model profile so agent routing can bind at least:

- provider/model/revision and agent/client/harness version;
- direct-model versus managed-agent execution kind;
- supported roles (orchestrator, planner, architect, designer, editor, verifier,
  reviewer, researcher, debugger, utility);
- chat, structured output, native/fallback tool use, parallel tools,
  continuation, resume, steering, cancellation and artifact-result support;
- context/output limits and multimodal support;
- measured planning, coding, architecture, UI/visual reasoning, C++, C#,
  debugging, repository editing, reverse engineering and summarization
  capabilities where relevant;
- provider health, rate/capacity state and evidence freshness;
- cost/resource class and permitted concurrent top-tier lanes;
- trust/egress zone and whether prompts/files leave the operator-controlled host.

Capability states are `declared -> tested -> passing/degraded/failed -> stale`.
The route planner may use only evidence sufficient for the requested role. A
vendor/model name or marketing capability is not routing evidence.

### Provider-independent handoff/result protocol

Add/extend typed contracts rather than free-form cross-provider chat:

```text
HandoffProposal
  source_worker_id
  objective
  requested_role
  required_capabilities
  context_policy/context_inputs
  constraints/success_criteria
  evidence_refs/artifact_refs
  suggested_provider (advisory only)
  inherited budget

ConsultationRequest / ConsultationResult
  bounded question
  accepted constraints
  selected source/artifact/verifier refs
  no mutation authority by default

AgentExecutionRoute
  provider/model/effort
  execution kind
  role/authority
  required capabilities
  capability-profile revision
  selection reason
  operator pin

StructuredChildResult
  status/conclusion
  evidence/artifact/verifier refs
  mutations/assumptions/unresolved questions
  usage
  provider/model/effort
  suggested next actions / delegation proposals
```

Provider-native text transcripts are not authoritative results and must not be
fanned into parent context by default.

### Implementation slices

| Slice | Work | Exit gate |
|---|---|---|
| P1 Contracts | Add provider-neutral agent authority/execution-kind/profile/route/handoff/consultation contracts around existing worker/delegation types. | Pure validation/serialization tests; no live provider calls. |
| P2 Dispatcher | Put the existing local provider behind a dispatching `SubagentProvider` registry with only `local` registered. | Existing local behavior byte/receipt compatible where required; no second registry. |
| P3 Capability routing | Extend MODEL-004/005/007/008/009 profiles and route explanation for managed-agent providers; observe-only recommendations first. | Recorded route eligibility/ineligibility reasons and stale-evidence canaries. |
| P4 First external advisor | Integrate one provider in read-only/scoped `ADVISOR` mode for architecture/review/consultation. | Credentials absent from prompts/results; no workspace mutation; restart-safe result binding. |
| P5 First external worker | Admit one mutating managed-agent connector only in an isolated worktree with owned scope and deterministic verification. | Crash/reconcile, cancellation, artifact digest/source revision and integration-gate canaries pass. |
| P6 Major provider set | Add OpenAI/Codex, Anthropic/Claude, xAI Grok model API, Google Gemini/Antigravity, GitHub Copilot, Cursor, Mistral and generic compatible direct-model adapters as their programmable surfaces qualify. | Each connector has a versioned conformance/evidence profile; unsupported capabilities remain false/unknown. |
| P7 Grok Bot bridge | Implement only if xAI exposes a stable supported programmatic Bot lifecycle suitable for host control. | No GUI automation; spawn/steer/status/cancel/result or equivalent lifecycle, durable identity and recovery semantics proven. |
| P8 Orchestrator role | Make the top-level orchestrator a configurable measured role (default deployment may prefer Astra Ultra) rather than a provider-specific authority. | Swapping eligible orchestrator providers changes policy only, not architecture or permissions. |
| P9 Operator surfaces | Expose provider inventory, agent tree, route reason, health, capability evidence, budgets, usage and handoffs through the shared control plane. | No endpoint/secret leakage; snapshots/events are resumable and reconstructable. |

### Connector acceptance canaries

Before any connector is considered production-capable, prove:

1. one provider cannot directly launch another provider;
2. a child may request a handoff but only Sonder can admit it;
3. user/model/provider pins never silently move;
4. provider failure never causes hidden cross-provider retry;
5. cloud/egress consent and credential scope cannot be widened by a child;
6. root worker/token/time/concurrency budgets remain conserved across providers;
7. provider-native descendants cannot mint unbounded Sonder resources;
8. duplicate resume/idempotency keys do not start duplicate external work;
9. scoped critics do not receive implementer rationale or full parent history by
   default;
10. provider credentials never enter prompts, worker metadata, returned artifacts
    or exported telemetry;
11. two workers cannot concurrently own overlapping production files;
12. stale source revisions/artifacts are refused at integration;
13. successful provider completion is not accepted when required deterministic
    verification is missing or failed;
14. crash after remote dispatch reconciles the original external run when
    possible and never blindly repeats an uncertain mutation;
15. cancellation distinguishes "request accepted" from proven quiescence/effect
    reversal;
16. provider degradation/rate limits do not corrupt unrelated provider state;
17. stale/synthetic/mismatched capability evidence makes a route ineligible;
18. an installation with only local models still satisfies the same role
    contracts without requiring any cloud connector;
19. an aggregator such as OpenRouter cannot hide an unrecorded provider switch;
20. Grok Bot or another provider-owned multi-agent service cannot use internal
    peer delegation to escape the single Sonder root budget/authority.

This work advances SEAM-009/015, AGENT-001/002/003/005/006/007/008/009/010,
MODEL-001/002/004/005/007/008/009, CTX-001/010, TOOL-001/003/007, OPS-001/004,
SEC-001/002 and API-001/006. This ledger entry does not mark any of those master
requirements complete. Live connectors remain disabled until their focused
implementation and provider-specific qualification evidence land.

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
