# Sonder Runtime master implementation specification

**Status:** Authoritative implementation plan
**Approved direction:** Consolidates the approved SPEC-5 end state and the 2026-08-19 harness audit
**Baseline:** `main` at `6670eaabe8ddb8a35c0c01e067f54d89c7379aeb`
**Scope:** Architecture, migration, runtime capabilities, verification, and release acceptance
**Implementation state:** Planning only; the checklist is intentionally unchecked until evidence is committed

## 1. Authority and use

This is the single implementation source of truth for the current Sonder Runtime
architecture program. It consolidates:

- `SPEC-5-End-State-Architecture.md`;
- `SPEC-5-MIGRATION-RUNBOOK.md`;
- `PROGRAM-STATUS.md`;
- the root architecture, client, security, self-modification, and training contracts;
- the 2026-08-19 comparison against DeepSeek Harness, Aider, OpenCode, Cline,
  and OpenHands.

The older documents remain as historical evidence or focused subsystem references.
If they conflict with this specification, this specification wins. ADRs record why a
decision was made; this document records what must be implemented and how completion
is proven.

Checkbox rules:

- `[ ]` means unverified, even if related code already exists.
- `[x]` requires committed evidence named in the item or linked from the evidence log.
- A feature is not complete merely because a class, test double, proposal, or alternate
  legacy path exists.
- Deleting obsolete behavior is part of completion, not optional cleanup.
- Documentation-only completion never proves runtime completion.

## 2. Product boundary and immutable decisions

- [ ] **CORE-001 — Runtime identity.** Preserve Sonder as a local-first runtime and
  orchestration system around models hosted by Ollama or an explicitly selected model
  provider. Do not describe Sonder as a foundation model.
- [ ] **CORE-002 — Modular monolith.** Preserve one deployable Python runtime with
  enforced internal boundaries; do not introduce microservices merely for separation.
- [ ] **CORE-003 — Single operator.** Optimize for one trusted operator on a workstation
  or privately controlled server; multi-tenant SaaS isolation is out of scope.
- [ ] **CORE-004 — Local by default.** Local models, memory, embeddings, tools, and state
  remain the default. A local failure must never silently trigger cloud inference.
- [ ] **CORE-005 — Preserve data, remove obsolete interfaces.** Supply crash-safe data
  migrations, but retain no permanent internal compatibility shims.
- [ ] **CORE-006 — Independent unrestricted authorities.** Keep startup-only
  `--unrestricted-tools` and `--unrestricted-selfmod` independent. Neither may be
  enabled after startup through HTTP, MCP, model output, automation, or another command.
- [ ] **CORE-007 — Attended training.** Unrestricted tools or self-modification must not
  authorize autonomous long-running training.
- [ ] **CORE-008 — Immutable deployments.** Trained model and adapter deployments use
  immutable identities; only the deployment service may change active routing.
- [ ] **CORE-009 — Signed updates.** Preserve signed update, backup, activation,
  health-gate, rollback, and standalone recovery behavior.
- [ ] **CORE-010 — No blind framework transplant.** Borrow capability seams and event
  discipline from other harnesses without rewriting Sonder in TypeScript or turning
  every internal function into a plugin.

## 3. Required end-state package architecture

Production behavior must live under `sonder_runtime/`:

```text
sonder_runtime/
  domain/          pure rules, entities, value objects, domain events
  application/     use cases, ports, orchestration, operation context
  adapters/        SQLite, Ollama/providers, files, Git, sandboxes, telemetry
  interfaces/      HTTP, MCP, CLI, REPL, SDK/event protocol translation
  platform/        typed config, paths, lifecycle, metrics, version
  bootstrap/       composition root and immutable startup capabilities
```

- [ ] **ARCH-001 — One implementation path.** Every core capability has exactly one
  authoritative production implementation.
- [ ] **ARCH-002 — Root cleanup.** Remove root-level business modules after their
  replacement slices are verified. Root files may remain only as launchers, packaging
  entrypoints, generated artifacts, or explicitly documented non-business utilities.
- [ ] **ARCH-003 — Transitional cleanup.** Remove strangler services, legacy adapters,
  deprecated application singletons, delegate modules, and alternate environment gates.
- [ ] **ARCH-004 — Pure domain.** Domain code performs no environment reads, filesystem
  mutation, SQLite, subprocess, socket, or network work.
- [ ] **ARCH-005 — Application ports.** Application use cases depend only on domain
  types and declared ports.
- [ ] **ARCH-006 — Adapter ownership.** All infrastructure failures are translated into
  typed application/domain errors at adapter boundaries.
- [ ] **ARCH-007 — Thin interfaces.** HTTP, MCP, CLI, REPL, Flutter, and SDK surfaces
  translate protocols and invoke application services; they do not implement workflows.
- [ ] **ARCH-008 — No import side effects.** Importing production modules opens no
  databases, creates no state directories, starts no threads, contacts no networks, and
  spawns no processes.
- [ ] **ARCH-009 — Typed configuration.** Bootstrap constructs immutable typed config;
  production modules do not repeatedly interpret environment variables.
- [ ] **ARCH-010 — Architecture enforcement.** CI rejects forbidden imports, dependency
  cycles, raw infrastructure use outside adapters, new root business modules, and legacy
  reintroduction.
- [ ] **ARCH-011 — Zero exceptions.** The final architecture checker contains no legacy
  allowlist or migration exceptions.
- [ ] **ARCH-012 — Ownership catalog.** Generate a machine-readable map of every package,
  owned state, public port, provider, schema, and lifecycle owner.
- [ ] **ARCH-013 — Split oversized modules.** Decompose orchestration, serving,
  workbench, and launcher/controller modules by bounded responsibility.

Completion evidence:

- [ ] `python scripts/check_architecture.py` exits 0 and reports zero exceptions.
- [ ] A CI test proves a synthetic root business module is rejected.
- [ ] A CI test proves a forbidden domain-to-infrastructure import is rejected.
- [ ] Repository inventory proves no duplicate production implementation remains.

## 4. Canonical runtime spine

Sonder will use one explicit runtime spine:

```text
session event log
  -> context/prompt assembly
  -> agent turn and step loop
  -> model gateway
  -> scoped tool gateway
  -> durable results, projections, telemetry, and UI events
```

### 4.1 Event-sourced sessions

- [ ] **SESSION-001 — Typed identities.** Define validated `SessionId`, `TurnId`,
  `StepId`, `CallId`, `AgentId`, `JobId`, `ArtifactId`, and `OperationId`.
- [ ] **SESSION-002 — Append-only log.** Persist an ordered append-only event stream for
  every session.
- [ ] **SESSION-003 — Durable vocabulary.** Record session lifecycle, user messages,
  injected context, prompt snapshots, model streaming, reasoning, tool calls/results,
  approvals, goals, plans, compaction, retrieval, subagents, cancellation, errors, and
  artifacts as typed events.
- [ ] **SESSION-004 — Model-visible invariant.** Everything sent to a model must be
  reconstructable from durable events.
- [ ] **SESSION-005 — Deterministic replay.** Rebuild a model request, transcript, UI,
  and operational projection from the log without hidden mutable state.
- [ ] **SESSION-006 — Forking.** Fork a session at an explicit event boundary while
  preserving lineage.
- [ ] **SESSION-007 — Resume and repair.** Resume valid sessions and diagnose/repair
  truncated or inconsistent tails without replaying unproven side effects.
- [ ] **SESSION-008 — Query and export.** Provide bounded session search, transcript
  export, event-range reads, and integrity inspection.
- [ ] **SESSION-009 — Projection checkpoints.** Persist versioned projection checkpoints
  for fast startup while retaining the event log as the source of truth.
- [ ] **SESSION-010 — Retention/privacy.** Apply explicit retention, redaction, export,
  and deletion policies per event/content class.

### 4.2 Turn, step, and cancellation semantics

- [ ] **LOOP-001 — Turn contract.** A turn begins when admitted work is claimed and ends
  only after all owed steps and accepted steering are reconciled.
- [ ] **LOOP-002 — Step contract.** A step is exactly one model request plus the tool calls
  and results caused by that request.
- [ ] **LOOP-003 — Interception events.** Expose typed `pre_step`, `model_request`,
  `pre_execute`, `execute`, `post_execute`, `turn_stopping`, and error/retry seams.
- [ ] **LOOP-004 — Durable versus live.** Clearly separate durable session facts from
  live interception/capability events.
- [ ] **LOOP-005 — Steering.** Support follow-up, immediate steering, passive context
  injection, cancellation, and stop with defined ordering.
- [ ] **LOOP-006 — Cancellation tree.** Propagate cancellation through streams, tools,
  subprocesses, terminals, subagents, jobs, training, updates, and selfmod verification.
- [ ] **LOOP-007 — Bounded retries.** Classify retryable failures, retain evidence, apply
  jitter/backoff where appropriate, and never retry destructive side effects blindly.
- [ ] **LOOP-008 — Idempotency.** Require idempotency keys or explicit reconciliation for
  resumable side-effecting operations.

## 5. Capability seams and lifecycle

Each seam has a typed contract, provider registry, owner, health state, configuration
schema, cancellation, cleanup-to-quiescence contract, and conformance tests.

- [ ] **SEAM-001 — ModelGateway** for all generation and streaming.
- [ ] **SEAM-002 — ToolRegistry/ToolExecutor** for every model-authored action.
- [ ] **SEAM-003 — FileSystem** with resource-aware policy and observations.
- [ ] **SEAM-004 — SubprocessRuntime/ShellExecutor/TerminalService** sharing one
  execution world.
- [ ] **SEAM-005 — SandboxProvider** for local, container, remote, and read-only worlds.
- [ ] **SEAM-006 — SessionRepository/SessionQueryEngine** for durable sessions.
- [ ] **SEAM-007 — CompactionEngine** over immutable session history.
- [ ] **SEAM-008 — SkillRegistry** with progressive discovery and policy.
- [ ] **SEAM-009 — SubagentProvider** for local children or explicitly configured
  external agents.
- [ ] **SEAM-010 — JobRegistry/WorkflowEngine** for background and resumable work.
- [ ] **SEAM-011 — WebProvider/CredentialProvider** with explicit egress and secret scope.
- [ ] **SEAM-012 — AttachmentStore/SpillStore** for bounded large outputs and artifacts.
- [ ] **SEAM-013 — TelemetrySink** with redaction before export.
- [ ] **SEAM-014 — EmbeddingProvider/TrainingBackend/UpdateActivator** for existing
  specialized lifecycles.
- [ ] **SEAM-015 — Scoped overrides.** An agent preset may replace a provider without
  creating a parallel global execution path.
- [ ] **SEAM-016 — Atomic lifecycle.** Provider initialization either publishes a fully
  usable capability or rolls back all registrations.

## 6. Context, prompt, and repository intelligence

### 6.1 Context planner

- [ ] **CTX-001 — One planner.** Replace independent prompt/context heuristics with one
  model-aware context planner.
- [ ] **CTX-002 — Section budgets.** Budget stable instructions, policy, goals/plans,
  tool schemas, skills, repository map, working files, recent history, memories,
  subagent results, and reserved output independently.
- [ ] **CTX-003 — Priority and eviction.** Define deterministic priorities, protected
  sections, eviction order, and emergency overflow behavior.
- [ ] **CTX-004 — Deduplication.** Remove exact and semantic duplication with provenance
  retained.
- [ ] **CTX-005 — Explainability.** Expose why each section/item was selected, its cost,
  source, confidence, and omission reason.
- [ ] **CTX-006 — Last-good snapshots.** Retain the last complete valid view when a
  provider returns an incomplete refresh.
- [ ] **CTX-007 — Overflow recovery.** Compact before overflow and use bounded adaptive
  shrinking after a proven overflow.
- [ ] **CTX-008 — Hardware-aware native context.** Size native context using measured
  model/KV-cache behavior rather than only global constants.
- [ ] **CTX-009 — Prefix caching.** Put stable instructions, schemas, project rules, and
  skill catalogs in reusable prefixes with versioned cache keys and hit/write metrics.
- [ ] **CTX-010 — Request replay.** Store the exact section manifest needed to reproduce
  a request.

### 6.2 Compaction

- [ ] **COMPACT-001 — Non-destructive.** Compaction appends an event and never replaces
  or deletes raw history by itself.
- [ ] **COMPACT-002 — Source range.** Bind every summary to the exact source event range.
- [ ] **COMPACT-003 — Structured retention.** Preserve facts, decisions, unresolved
  tasks, artifacts, tool outcomes, and confidence separately.
- [ ] **COMPACT-004 — Validation.** Evaluate factual retention and allow re-compaction
  from original events.
- [ ] **COMPACT-005 — Typed modalities.** Do not flatten reasoning, images, tools, or
  attachments into undifferentiated text.

### 6.3 Repository intelligence

- [ ] **REPO-001 — Incremental symbol index.** Use Tree-sitter where available to index
  definitions, references, imports, inheritance, calls, and build targets.
- [ ] **REPO-002 — Language baseline.** Support C++, C#, Python, Rust, TypeScript,
  JavaScript, Java, Go, and relevant project/build formats.
- [ ] **REPO-003 — Ranked repository map.** Fit the most task-relevant symbols and
  relationships into an explicit token budget.
- [ ] **REPO-004 — LSP integration.** Use LSP navigation when available, with indexed and
  lexical fallback.
- [ ] **REPO-005 — Evidence revisions.** Bind repository evidence to exact file digests
  and Git revisions.
- [ ] **REPO-006 — Progressive expansion.** Let agents expand around symbols, callers,
  callees, types, tests, and build targets.
- [ ] **REPO-007 — Multi-root.** Support cross-repository read context while retaining
  explicit write ownership and independent Git histories.

## 7. Skills and extensions

### 7.1 Skills

- [ ] **SKILL-001 — Registry.** Discover bundled, global, project, and configured skill
  roots with deterministic precedence.
- [ ] **SKILL-002 — Progressive disclosure.** Initially expose only validated names and
  concise descriptions; load full content with `skill(name)`.
- [ ] **SKILL-003 — Live refresh.** Watch or safely poll skill sources, digest complete
  catalogs, and preserve the last-good view on incomplete scans.
- [ ] **SKILL-004 — Trust and policy.** Record source, trust, version, compatibility,
  model-invocable policy, and required permissions.
- [ ] **SKILL-005 — Health.** Disable or quarantine malformed and repeatedly failing
  skills without preventing runtime startup.
- [ ] **SKILL-006 — Procedural promotion.** Promote evidence-backed learned workflows to
  versioned candidate skills only after held-out evaluation.

### 7.2 Plugin-lite extensions

- [ ] **EXT-001 — Extension boundary.** Permit extensions for models, tools, skills,
  retrievers, embedders, sandboxes, training backends, artifact generators, workflows,
  telemetry sinks, and UI panels—not arbitrary internal functions.
- [ ] **EXT-002 — Manifest.** Require identity, version, protocol compatibility,
  dependencies, permissions, configuration schema, health probe, and cleanup contract.
- [ ] **EXT-003 — Isolation.** Prefer out-of-process hosts for untrusted/native extension
  code and bound startup, memory, time, output, and restart frequency.
- [ ] **EXT-004 — Quarantine.** Automatically quarantine incompatible or repeatedly
  crashing extensions and expose repair diagnostics.
- [ ] **EXT-005 — Inventory.** Provide project/global installation state, provenance,
  updates, disablement, and health in CLI/API/UI.
- [ ] **EXT-006 — Ephemeral experiments.** Behind explicit startup authority, permit
  inspect/define/start/stop/delete of temporary runtime experiments in a child process.
- [ ] **EXT-007 — No automatic promotion.** Temporary experiments never become
  persistent code or configuration without the normal guarded implementation workflow.

## 8. Agents, workflows, and jobs

- [ ] **AGENT-001 — Unified registry.** Define one create/resume/fork/send/steer/inject/
  cancel/stop/dispose/status contract.
- [ ] **AGENT-002 — Migrate existing modes.** Fleet, Autopilot, Workbench, review, and
  self-improvement consume the shared contract rather than owning alternate loops.
- [ ] **AGENT-003 — Presets.** Presets bind model route, prompt sections, tools,
  permissions, skills, memory, sandbox, context planner, and budgets.
- [ ] **AGENT-004 — Built-ins.** Supply general, code, plan, reviewer, researcher,
  debugger, reverse-engineer, build/test, utility, vision, and self-improvement presets.
- [ ] **AGENT-005 — Durable lineage.** Persist parent/child/delegation depth and expose
  read-only descendant discovery.
- [ ] **AGENT-006 — Continuable children.** Continue a child later using its durable
  session rather than copying a textual summary into a new agent.
- [ ] **AGENT-007 — Budgets.** Enforce depth, child count, concurrency, tokens, time,
  model, and execution-resource budgets.
- [ ] **AGENT-008 — Isolated workspaces.** Assign explicit read/write workspaces and
  reconcile concurrent Git changes without force-overwriting another session.
- [ ] **AGENT-009 — Structured delegation.** Child tasks and results use declared
  contracts with evidence and artifact references.
- [ ] **AGENT-010 — Architect/editor/reviewer.** Support explorer, architect, editor,
  verifier, reviewer, and integrator roles with independently routed models.

- [ ] **JOB-001 — Generic registry.** Route background shell, terminal, workflow,
  subagent, training, update, and selfmod verification work through one job contract.
- [ ] **JOB-002 — Control.** Provide start/list/poll/stream/cancel/collect and durable
  parent session/operation linkage.
- [ ] **JOB-003 — Recovery.** Reconcile running, orphaned, interrupted, and resumable jobs
  after restart.
- [ ] **JOB-004 — Process containment.** Termination reaches the full process tree.
- [ ] **JOB-005 — Output watermarks.** Stream bounded output with cursors and spill large
  payloads instead of repeating them.

## 9. Tools, permissions, and execution worlds

- [ ] **TOOL-001 — One gateway.** Every model-facing tool from HTTP, MCP, Workbench,
  Fleet, Autopilot, skills, and selfmod passes through `ToolService`.
- [ ] **TOOL-002 — Pipeline.** Validate schema, resolve scope, evaluate permission,
  acquire approval, normalize arguments, execute with deadline/cancellation, bound output,
  redact, issue a receipt, and append durable events.
- [ ] **TOOL-003 — Resource-aware policy.** Match tool, operation, path/host/resource,
  agent preset, workspace, origin, side-effect class, persistence, and secret exposure.
- [ ] **TOOL-004 — Decisions.** Support allow, ask, deny, allow-once, session/project
  grants, sandbox-only, and attended-only outcomes.
- [ ] **TOOL-005 — Immutable startup authorities.** Unrestricted capabilities are
  captured by bootstrap and cannot be mutated at runtime.
- [ ] **TOOL-006 — Generated catalogs.** Generate MCP/OpenAI schemas, CLI help, client
  forms, documentation, permission metadata, and conformance fixtures from authoritative
  contracts; CI rejects drift.
- [ ] **TOOL-007 — Receipts.** Record requester, policy match, approval, resource,
  argument/result digests, execution world, effects, timing, model, and schema version.

- [ ] **EXEC-001 — Shared execution world.** Filesystem, shell, subprocess, terminal,
  LSP, and code execution resolve to the same local/container/remote world.
- [ ] **EXEC-002 — Container default.** Generated code, generated tests, and guarded
  selfmod execute in Docker or Podman by default when available and required by policy.
- [ ] **EXEC-003 — Persistent terminal.** Provide start/send/resize/read/stop,
  reconnection, watermarks, idle timeout, and UI presentation.
- [ ] **EXEC-004 — Spill store.** Represent large output as a digest-bound reference with
  preview, size, MIME type, owner, range reads, search, and retention.
- [ ] **EXEC-005 — Remote execution.** Allow a configured remote worker without changing
  tool semantics or silently widening filesystem/network authority.
- [ ] **EXEC-006 — Isolation claims.** Clearly distinguish failure isolation from a real
  security boundary in documentation and receipts.

## 10. Memory and learning

- [ ] **MEM-001 — Memory classes.** Separate working, episodic, semantic, procedural,
  preference, project, failure, and artifact memory.
- [ ] **MEM-002 — Per-class policy.** Define write criteria, confidence, provenance,
  retrieval, privacy, decay, promotion, export, and deletion for each class.
- [ ] **MEM-003 — Evidence-backed experience.** Store baseline, strategy delta,
  deterministic evidence, outcome, assumptions, applicability boundary, confidence, and
  counterexamples—not unverified model assertions.
- [ ] **MEM-004 — Temporal truth.** Support valid-from/until, supersedes, contradictions,
  source trust, decay, and revalidation.
- [ ] **MEM-005 — Retrieval explanation.** Return selection score components, provenance,
  freshness, confidence, and exclusion reasons.
- [ ] **MEM-006 — Vector-space identity.** Bind embeddings to model, revision,
  dimensions, normalization, truncation, and serving implementation.
- [ ] **MEM-007 — Quality evaluation.** Maintain labeled recall sets and measure
  relevance, contradiction rate, stale recall, latency, and context cost.
- [ ] **MEM-008 — Procedural skill promotion.** Convert repeatable proven workflows into
  candidate skills with baseline comparison and rollback.

## 11. Evaluation and self-improvement

- [ ] **EVAL-001 — First-class evaluation domain.** Promote the self-evaluation proposal
  into owned application/domain/adapters with immutable run records.
- [ ] **EVAL-002 — Suites.** Cover fixed tasks, repository tasks, tool use, memory,
  grounding, continuation, permission/sandbox attacks, and recovery.
- [ ] **EVAL-003 — Metrics.** Measure correctness, evidence, regressions, latency, tokens,
  cost, tool calls, retries, and resource use.
- [ ] **EVAL-004 — Dimensions.** Bind results to model digest, route, prompt manifest,
  skill catalog, tool catalog, runtime version, hardware, and environment.
- [ ] **EVAL-005 — Trajectory replay.** Replay recorded sessions with alternate models,
  prompts, skills, routers, or compaction while safely substituting recorded side effects.
- [ ] **EVAL-006 — Divergence.** Identify the earliest meaningful decision divergence and
  retain minimized reproducible failures.
- [ ] **EVAL-007 — Promotion gates.** Define thresholds and confidence requirements for
  runtime, prompt, skill, route, model, memory, and selfmod promotion.
- [ ] **EVAL-008 — Shadow/canary.** Compare candidate behavior without user-visible
  effect, canary eligible work, and automatically demote measured regressions.
- [ ] **EVAL-009 — Proposal lifecycle.** Track proposed, accepted, implementing,
  experimental, shadow, production, rejected, and superseded states with owners and exit
  criteria.

- [ ] **SELFMOD-001 — Evidence first.** Require a concrete problem, reproducible failure
  or benchmark where practical, and a scoped proposed outcome.
- [ ] **SELFMOD-002 — Isolated candidate.** Use a clean worktree/snapshot and preserve
  the operator's concurrent changes.
- [ ] **SELFMOD-003 — Verification.** Run targeted, architecture, selected regression,
  security, and before/after evaluation gates.
- [ ] **SELFMOD-004 — Review and deployment.** Inspect the diff/evidence, back up,
  activate atomically, health-check, and retain standalone rollback.
- [ ] **SELFMOD-005 — Unrestricted truthfulness.** When unrestricted selfmod bypasses
  gates, report exactly what was bypassed; retain deadlines, cancellation, cleanup,
  output bounds, operation IDs, and logging.
- [ ] **SELFMOD-006 — No automatic push.** Guarded selfmod may create a local descriptive
  commit from a clean starting checkout, but never pushes remotely by itself.

## 12. Model routing, inference, and hardware

- [ ] **MODEL-001 — One gateway.** All generation, including local and explicitly
  selected hosted routes, uses `ModelGateway`.
- [ ] **MODEL-002 — Pure route planner.** Route planning performs no model/network I/O
  and returns an explainable decision.
- [ ] **MODEL-003 — Logical roles.** Preserve fast, code, general, reasoning, and vision
  tiers and router, workbench, autopilot, fleet, and review lanes while permitting
  planner/editor/verifier role composition.
- [ ] **MODEL-004 — Calibration profiles.** Key measured residency, throughput, latency,
  and KV-cache costs by exact model digest, quantization, architecture, total/active
  parameters, context, backend, parallelism, and hardware.
- [ ] **MODEL-005 — Capability profiles.** Measure planning, editing, tools, structured
  output, long context, C++, C#, reverse engineering, vision, and summarization.
- [ ] **MODEL-006 — MoE correctness.** Use total parameters for residency and active
  parameters plus measurements for decode behavior.
- [ ] **MODEL-007 — Controlled escalation.** Escalate after uncertainty/verifier failure
  only to routes explicitly permitted for the request; record whether escalation helped.
- [ ] **MODEL-008 — Separate role budgets.** Independently route planner, editor,
  verifier, summarizer/compactor, embedder, and vision work.
- [ ] **MODEL-009 — Provider health.** Distinguish configured, available, ready,
  degraded, and failed states without silently changing privacy policy.
- [ ] **MODEL-010 — NPU boundary.** Keep NPU acceleration below generative tiers as an
  optional utility provider with exact vector-space identity and CPU-fallback truthfulness.

## 13. API, MCP, SDK, and clients

- [ ] **API-001 — Shared event vocabulary.** Text/reasoning deltas, plans, goals, tools,
  approvals, jobs, agents, artifacts, warnings, errors, usage, and compaction use one typed
  protocol across clients.
- [ ] **API-002 — Resumable streams.** Provide monotonic sequence numbers, snapshot plus
  events, resume watermarks, idempotent commands, duplicate suppression, and backpressure.
- [ ] **API-003 — MCP.** Keep MCP 2.x current, test legacy-era negotiation only where it
  remains a supported external protocol contract, and deliver notifications through the
  negotiated subscription mechanism.
- [ ] **API-004 — OpenAI compatibility.** Implement and test supported Chat Completions
  and Responses semantics without bypassing Sonder policy and event recording.
- [ ] **API-005 — Editor/agent interoperability.** Support an explicit agent/editor
  protocol and import/export for `AGENTS.md`, `SKILL.md`, and common rule formats.
- [ ] **API-006 — Operator control plane.** Expose sessions, goals/plans, approvals,
  jobs, agent tree, model/hardware, context, memory explanations, extensions/skills,
  training, selfmod, updates, health, and startup authorities.
- [ ] **API-007 — Mobile parity.** The Flutter client can reconnect, resume streams,
  control a remote private host, and display the same durable state as desktop/web/CLI.
- [ ] **API-008 — Schema generation.** Client and SDK schemas derive from the runtime
  event and command contracts and are freshness-gated in CI.

## 14. Persistence, operations, and recovery

- [ ] **DATA-001 — Per-domain SQLite.** Each persistent domain owns its database,
  migrations, repository, and transaction semantics.
- [ ] **DATA-002 — No cross-database transactions.** Coordinate domains through
  application workflows and durable events.
- [ ] **DATA-003 — Transactional outbox.** Persist state changes and emitted durable
  events atomically in every state-owning domain.
- [ ] **DATA-004 — Compare-and-set.** Use revision checks on persistent workflow and
  state-machine aggregates.
- [ ] **DATA-005 — Crash-safe migration.** Verify a backup before destructive adoption or
  schema migration and prove restore independently per domain.
- [ ] **DATA-006 — Epoch adoption.** Complete schema epoch 2 adoption, delete temporary
  bridge code, and reject unsupported future schemas explicitly.
- [ ] **DATA-007 — Immutable artifact hashes.** Bind training, selfmod, update, session
  attachments, and generated deliverables to full hashes and manifests.

- [ ] **OPS-001 — Operation context.** Correlate inbound requests through inference,
  tools, persistence, agents, training, selfmod, and updates.
- [ ] **OPS-002 — Structured tracing.** Add redact-before-export tracing compatible with
  OpenTelemetry for requests, turns, steps, model calls, retrieval, tools, jobs, and
  lifecycle operations.
- [ ] **OPS-003 — Health model.** Distinguish liveness, readiness, dependency health,
  degraded capability, drain state, and recovery-required state.
- [ ] **OPS-004 — Startup reconciliation.** Repair or classify interrupted sessions,
  operations, tool calls, jobs, subagents, outboxes, migrations, and activations.
- [ ] **OPS-005 — Graceful drain.** Stop admitting work, communicate deadlines, settle or
  cancel descendants, flush state, and prove process-tree cleanup.
- [ ] **OPS-006 — Bounded telemetry.** Never export prompt, memory, secret, or artifact
  content unless explicitly configured; bound labels and cardinality.

## 15. Security

- [ ] **SEC-001 — Credential broker.** Give tools scoped credential handles instead of
  broadly injecting raw secrets.
- [ ] **SEC-002 — Egress policy.** Apply per-tool host/protocol/network rules and protect
  loopback, link-local, metadata, redirect, DNS-rebinding, and private-network targets.
- [ ] **SEC-003 — Filesystem races.** Use symlink- and race-resistant path resolution for
  authorized roots and destructive targets.
- [ ] **SEC-004 — Package/archive safety.** Bound files, expansion ratios, paths, links,
  total bytes, and parser resource use.
- [ ] **SEC-005 — Extension provenance.** Record trust/signature/source, produce an SBOM,
  and quarantine compromised or incompatible packages.
- [ ] **SEC-006 — Prompt-injection provenance.** Label untrusted retrieved/tool/web
  content and prevent it from silently becoming policy or memory.
- [ ] **SEC-007 — Secret scanning.** Scan contributions, model-authored patches,
  training exports, logs, artifacts, and publication candidates.
- [ ] **SEC-008 — Fuzzing.** Fuzz protocol decoders, tool schemas, patch application,
  migrations, archives, and policy matchers.
- [ ] **SEC-009 — Recovery boundary.** Never claim same-user recovery or audit files are
  a security boundary against explicitly unrestricted selfmod.

## 16. Training and deployment

- [ ] **TRAIN-001 — Reproducible manifest.** Bind dataset snapshot, base model,
  tokenizer, dependencies, backend, seed, hyperparameters, checkpoints, and hardware.
- [ ] **TRAIN-002 — Qualified lock.** Maintain an exact training dependency lock separate
  from runtime dependencies and verify the execution environment before training.
- [ ] **TRAIN-003 — Dataset provenance.** Validate privacy, license/source, deduplication,
  contamination, quality, and train/eval separation.
- [ ] **TRAIN-004 — Evaluation gate.** Compare behavior, regressions, latency, memory,
  context, and tool use before deployment.
- [ ] **TRAIN-005 — Immutable deployment.** Hash and name adapters/models immutably;
  mutable aliases are convenience pointers only.
- [ ] **TRAIN-006 — Deployment service.** Only attended deployment may change active
  routing after load, inference, compatibility, and evaluation checks.
- [ ] **TRAIN-007 — Adapter catalog.** Support task, project, and personalization adapters
  with compatibility and explicit composition rules.
- [ ] **TRAIN-008 — Cheap learning first.** Prefer memory, retrieval, skills, routing, and
  few-shot changes before weight training when they can encode the behavior reliably.
- [ ] **TRAIN-009 — Rollback.** Retain prior active routes, checkpoints, manifests, and a
  tested rollback path.

## 17. Updates and release engineering

- [ ] **UPDATE-001 — Bounded updates domain.** Move update state, TUF verification,
  download, staging, activation, health gate, rollback, and history under one domain.
- [ ] **UPDATE-002 — Platform activation.** Complete and test helper-process activation
  and self-replacement on Windows and macOS in addition to Linux.
- [ ] **UPDATE-003 — Runtime contract.** Verify exact sealed-runtime dependency contracts
  before stamping or activating a bundle.
- [ ] **UPDATE-004 — Atomic activation.** A failed activation restores the previous known
  good release without relying on the failed runtime.
- [ ] **UPDATE-005 — Signed release evidence.** Publish hashes, manifests, SBOM, test
  results, migration requirements, and rollback compatibility.

## 18. Documentation consolidation and governance

- [ ] **DOC-001 — Authority index.** Maintain `docs/architecture/README.md` as the map of
  authoritative, focused, and historical documents.
- [ ] **DOC-002 — Historical labeling.** Mark SPEC-5, its migration runbook, and old
  program status as superseded without erasing decision history.
- [ ] **DOC-003 — ADR namespace.** Consolidate new ADRs under one directory with globally
  unique IDs; retain the two old directories as historical series until normalized.
- [ ] **DOC-004 — Focused contracts.** Keep `ARCHITECTURE.md`, `SECURITY.md`,
  `SELFMOD.md`, `TRAINING.md`, `CLIENT.md`, and `MOBILE_HOST_CONTROL.md` focused on
  current behavior and link their unfinished implementation work here.
- [ ] **DOC-005 — Generated references.** Generate tool, command, event, configuration,
  schema, and capability references from source and freshness-gate them.
- [ ] **DOC-006 — Status evidence.** Check off an item only in the same change that adds
  or links its verifiable evidence.
- [ ] **DOC-007 — No stale promises.** Product documentation distinguishes implemented,
  experimental, proposed, degraded, and unsupported behavior.

## 19. Implementation sequence

Work packages are ordered. A later package may be researched early, but production code
must not create another parallel architecture to avoid an unfinished prerequisite.

### WP0 — Baseline and documentation authority

- [ ] Publish this specification and authority index.
- [ ] Label superseded documents.
- [ ] Capture current architecture exceptions, root-module inventory, databases,
  interfaces, tools, and state locations in machine-readable form.
- [ ] Add the evidence log format and requirement-ID checks.

### WP1 — Finish the SPEC-5 package migration

- [ ] Complete the package composition root and typed configuration.
- [ ] Move remaining production behavior under `sonder_runtime/` slice by slice.
- [ ] Remove root delegates, strangler services, duplicate paths, and legacy exceptions.
- [ ] Make architecture enforcement zero-exception.

### WP2 — Canonical sessions and agent loop

- [ ] Implement session identities, event schema, repository, replay, and projections.
- [ ] Implement turn/step/tool lifecycle and unified cancellation.
- [ ] Adapt existing interfaces to the canonical loop without changing policy semantics.

### WP3 — Capability seams and unified tools

- [ ] Establish provider lifecycle and scoped registration.
- [ ] Unify model and tool gateways.
- [ ] Establish execution worlds, jobs, terminals, spills, and permission receipts.

### WP4 — Context, repository intelligence, and skills

- [ ] Implement context planner and non-destructive compaction.
- [ ] Implement incremental repository map and LSP seam.
- [ ] Implement progressive skill discovery and plugin-lite manifests.

### WP5 — Unified agents and workflows

- [ ] Migrate Fleet, Autopilot, Workbench, and review to the agent registry.
- [ ] Add durable continuable subagents, presets, budgets, and architect/editor/reviewer.
- [ ] Add restart-safe workflows and generic jobs.

### WP6 — Memory, evaluation, and self-improvement

- [ ] Complete typed memory classes and evidence-backed procedural learning.
- [ ] Promote evaluation and trajectory replay.
- [ ] Gate skills, routing, memory, models, and selfmod through measured promotion.

### WP7 — Model/hardware calibration and training

- [ ] Replace static fit/performance assumptions with measured calibration profiles.
- [ ] Add capability-based role routing and controlled escalation.
- [ ] Complete reproducible training, adapter catalog, immutable deployment, and rollback.

### WP8 — Interfaces and operator control plane

- [ ] Publish the shared event/command protocol and resumable streams.
- [ ] Bring MCP, OpenAI compatibility, SDK/editor protocol, and Flutter surfaces onto it.
- [ ] Complete operator views for context, memory, agents, jobs, capabilities, and recovery.

### WP9 — Security, operations, updates, and release

- [ ] Complete tracing, startup reconciliation, credential/egress hardening, and fuzzing.
- [ ] Complete cross-platform update activation and standalone rollback tests.
- [ ] Run full acceptance, migration rehearsal, soak, performance, and recovery suites.

## 20. Verification matrix

Every implementation PR must select applicable rows and attach evidence.

| Area | Minimum evidence |
|---|---|
| Architecture | checker, import-cycle scan, root/legacy inventory diff |
| Behavior | targeted tests plus selected regression suite |
| Persistence | migration, rollback, crash/fault-injection, future-schema rejection |
| Events | schema compatibility, replay equivalence, ordering/idempotency |
| Model | fake-provider contract tests plus live local smoke where available |
| Tools | schema conformance, permission matrix, cancellation, receipt, output bounds |
| Execution | containment, timeout, process-tree cleanup, path/egress policy |
| Context | token accounting, selection explanation, overflow and compaction replay |
| Memory | labeled retrieval evaluation, contradiction/staleness cases |
| Agents/jobs | lineage, budgets, concurrent steering, restart recovery |
| Security | abuse cases, secret scan, archive/path/network boundaries |
| Training | immutable manifest, held-out evaluation, deployment rollback |
| Updates | signature/TUF, dependency contract, activation fault, external rollback |
| Client/API | generated schema freshness, reconnect/resume, backpressure |
| Performance | before/after benchmark with hardware and model identity |

Repository-wide release commands must be maintained in the runbook and CI. At minimum:

```bash
python scripts/check_architecture.py
python scripts/check_history_privacy.py
python -m pytest -q
python -m ruff check .
```

Platform and optional-dependency suites must report `passed`, `failed`, or `not run`
with a reason; absence of the environment is not evidence of passing.

## 21. Evidence log

Add entries only when a checkbox changes to `[x]`.

The machine-readable ledger design is specified in
[`EVIDENCE-TRACKING-DESIGN.md`](EVIDENCE-TRACKING-DESIGN.md). The current read-only
baseline is recorded in [`WP0-BASELINE.md`](WP0-BASELINE.md) and
[`wp0-baseline.json`](wp0-baseline.json).

| Requirement | Commit/PR | Evidence | Notes |
|---|---|---|---|
| _none yet_ | — | — | Planning baseline only |

## 22. Definition of done

The architecture program is complete only when all of the following are checked:

- [ ] Every Must-level requirement in this document is implemented and evidenced.
- [ ] Production behavior has one authoritative path under `sonder_runtime/`.
- [ ] Root compatibility business modules and transitional adapters are absent.
- [ ] Session replay reconstructs every model-visible fact and consequential tool result.
- [ ] Model, tool, agent, job, memory, training, selfmod, update, and client paths use the
  canonical contracts.
- [ ] Unrestricted authorities are startup-only, independent, truthful, and tested.
- [ ] Data migration and rollback are rehearsed from a representative pre-epoch backup.
- [ ] Full test, architecture, security, release, recovery, and platform matrices pass.
- [ ] Performance and reliability meet recorded baselines without unexplained regressions.
- [ ] Documentation contains no competing active implementation specification.

## 23. External design references

These are design inputs, not dependencies or claims of code reuse:

- DeepSeek Harness architecture and event/session spine:
  <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/architecture.md>
- DeepSeek capability seams:
  <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/capability-seams.md>
- DeepSeek subsystem and event maps:
  <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/README.md>
  and <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/event-producer-consumer.md>
- DeepSeek progressive skills:
  <https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md>
- DeepSeek temporary runtime extension trust model:
  <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/extensions/tool-cordis/README.md>
- Aider repository maps and architect/editor separation:
  <https://aider.chat/docs/repomap.html> and
  <https://aider.chat/2024/09/26/architect.html>
- OpenCode agents, skills, plugins, and permissions:
  <https://opencode.ai/docs/agents/>, <https://opencode.ai/docs/skills/>,
  <https://opencode.ai/docs/plugins/>, and <https://opencode.ai/docs/permissions/>
- Cline checkpoints and rules:
  <https://docs.cline.bot/core-workflows/checkpoints> and
  <https://docs.cline.bot/customization/cline-rules>
- OpenHands sandbox/control-plane boundary:
  <https://docs.openhands.dev/overview/introduction>

## 24. Internal references

- `ARCHITECTURE.md` — product and current high-level runtime boundary.
- `SECURITY.md` and `docs/security/` — current security and isolation contracts.
- `SELFMOD.md` — current guarded/unrestricted selfmod behavior.
- `TRAINING.md` — current adapter training and deployment behavior.
- `CLIENT.md` and `MOBILE_HOST_CONTROL.md` — current client/host contracts.
- `docs/adr/` and `docs/architecture/adr/` — historical architectural decisions.
- `docs/architecture/migration-inventory.json` — migration inventory input; it must be
  regenerated and eventually prove zero legacy exceptions.
- `docs/superpowers/specs/` and `proposals/` — research inputs; neither overrides this
  specification until promoted through the proposal lifecycle.
