# WP1 slice log formerly appended to the root README

**Classification:** historical implementation history. This file is not a
current product contract.

These notes were appended to the end of the root `README.md` while WP1 moved
implementation ownership into `sonder_runtime/`. They were relocated verbatim
so the product README describes current behavior only. Individual notes record
what a slice did at the time it landed; later slices may have retired the
compatibility aliases they mention. For current requirement status, use the
[master specification](SONDER-MASTER-IMPLEMENTATION-SPEC.md) and the
[generated requirement status](generated/requirement-status.md); for document
classifications, use the
[document authority index](DOCUMENT-AUTHORITY-INDEX.md).

## Relocated notes (original order)

- WP1 Forty-Fifth Slice: the HTTP chat usage presentation helper now lives in `sonder_runtime.adapters.observability.chat_formatting`.
- WP1 Forty-Sixth Slice: the pure REPL duration presentation helper now lives in `sonder_runtime.adapters.observability.repl_formatting`.
- WP1 Forty-Seventh Slice: the active architecture legacy-root policy now covers only roots with live package callers; `autopilot_store` is migration-only.
- WP1 Forty-Eighth Slice: pure HTTP command-completion limit normalization now lives in `sonder_runtime.adapters.command_completion`.
- WP1 Fifty-First Slice: pure runtime model-readiness presentation now lives in `sonder_runtime.adapters.runtime_readiness_formatting`.
- WP1 Fifty-Second Slice: pure run-result presentation now lives in `sonder_runtime.adapters.observability.run_result_formatting`.
- WP1 Fifty-Fifth Slice: the Ollama gateway now reads its endpoint through the packaged `sonder_runtime.adapters.ollama.endpoint` boundary; the remaining server model-routing dependency is explicit.
- WP1 Fifty-Fourth Slice: pure goal presentation now lives in `sonder_runtime.adapters.goal_formatting`.
- WP1 Fifty-Sixth Slice: pure learning-tier model/provider presentation now lives in `sonder_runtime.adapters.learning_tier_formatting`.
- WP1 Sixty-Seventh Slice: the embedding-cache adapter now consumes database paths through `sonder_runtime.platform.paths`.
- WP1 Fifty-Seventh Slice: the import-time model/tier seed now has a typed immutable projection in `sonder_runtime.domain.runtime_model_configuration`; server compatibility aliases and live policy refresh remain intact.
- WP1 Fifty-Eighth Slice: Ollama process-lifecycle policy now lives in the packaged `sonder_runtime.adapters.ollama_lifecycle` boundary; the root module remains a compatibility import.
- WP1 Fifty-Ninth Slice: the live cloud-default compatibility repair now consumes its replacement from the frozen typed runtime-model configuration projection.
- WP1 Sixtieth Slice: the root Ollama lifecycle compatibility import now exposes only the packaged adapter's two public helpers; private process and trust hooks remain package-internal.
- WP1 Seventy-Fifth Slice: the filesystem workbench now consumes the canonical `sonder_runtime.platform.logging` seam, preserving handler setup and redaction semantics.
- WP1 Eighty-First Slice: filesystem operations now consume the canonical `sonder_runtime.platform.paths` seam, preserving default-home resolution and containment semantics.
- WP1 Seventy-Seventh Slice: local observability now consumes the canonical `sonder_runtime.platform.logging` seam, preserving logger identity and redaction semantics.
  - WP1 Seventy-First Slice: the packaged preference adapter now resolves its default memory database path through `sonder_runtime.platform.paths`.
- WP1 Sixty-First Slice: the preflight adapter now consumes `SonderConfig` through `sonder_runtime.platform.config`, preserving the root implementation's environment/default semantics while reducing a package caller's root dependency.
- WP1 Sixty-Fourth Slice: the HTTP interface now reads the API-key policy through `sonder_runtime.platform.config`, preserving the canonical configuration defaults and environment-backed implementation.
- WP1 Sixty-Third Slice: the packaged entrypoint now consumes typed configuration through `sonder_runtime.platform.config`, preserving the historical `sonder_paths` compatibility attribute.
- WP1 Sixty-Sixth Slice: the evaluation-history adapter now consumes state locations through `sonder_runtime.platform.paths`, preserving existing path resolution and migration behavior.
- WP1 Sixty-Eighth Slice: local-system packaging now proves that retired `eval_history.py` is excluded while the canonical evaluation-history store and application package are included.
- WP1 Sixty-Ninth Slice: the runtime-policy adapter now consumes its state-file path through `sonder_runtime.platform.paths`, preserving explicit policy-path overrides and legacy home resolution.
- WP1 Seventieth Slice: the packaged HTTP lifecycle now consumes the shutdown coordinator through `sonder_runtime.platform.shutdown`, preserving the root implementation and all drain semantics.
- WP1 Seventy-Second Slice: the packaged HTTP lifecycle now consumes `MetricsRegistry` through `sonder_runtime.platform.metrics`, preserving metric names, labels, and semantics.
- WP1 Seventy-Fourth Slice: the packaged backup adapter now consumes build identity through `sonder_runtime.platform.version`, preserving stamped version and commit metadata.
- WP1 Sixty-Fifth Slice: the read-only doctor checks now load typed configuration through `sonder_runtime.platform.config`, preserving diagnostic output and error handling.
- WP1 Fiftieth Slice: the active architecture legacy-root policy now excludes `fleet_store`; its root alias remains only for immutable migration replay.
WP1 Seventy-Eighth Slice: the packaged NPU manifest adapter now resolves its manifest directory through the canonical platform path seam.
WP1 Seventy-Ninth Slice: the NPU service now resolves its shadow-ledger state file through the canonical platform path seam.

WP1 Eighty-Eighth Slice: the operations store now consumes redaction through the canonical `sonder_runtime.platform.logging` seam, preserving durable event persistence semantics.
WP1 Ninetieth Slice: the strangler unit-of-work now resolves its default memory database through `sonder_runtime.platform.paths`, preserving the live memory-store port and explicit path overrides.
WP1 Ninety-First Slice: the filesystem workbench now resolves its Bash executable through `sonder_runtime.platform.paths`, preserving workspace resolution and containment semantics.
WP1 Eighty-Ninth Slice: the migrations adapter now resolves the operations database through the canonical `sonder_runtime.platform.paths` seam; immutable migration replay and database locations are unchanged.
WP1 Eightieth Slice: the packaged secret-rotation adapter now resolves its default rotation state through the canonical platform path seam.
WP1 Eighty-Second Slice: the update engine now reads build identity through the canonical platform version seam; signed-update verification and release metadata behavior are unchanged.
WP1 Eighty-Third Slice: workflow persistence now resolves its mutable state home through the canonical platform path seam; workspace overrides, containment, legacy migration, and atomic writes are unchanged.
WP1 Eighty-Fourth Slice: the update service now reads build identity through the canonical platform version seam; bundle metadata and signed-update verification behavior are unchanged.
WP1 Eighty-Sixth Slice: fleet persistence now resolves its database and principal-credential paths through the canonical `sonder_runtime.platform.paths` seam; SQLite and migration semantics are unchanged.
- WP1 Eighty-Seventh Slice: queued-action persistence now resolves its database path through the canonical `sonder_runtime.platform.paths` seam; queue and immutable migration semantics are unchanged.
- WP1 Ninety-Second Slice: the update engine now resolves default release and active-pointer paths through the canonical `sonder_runtime.platform.paths` seam; signed-update verification and bootstrap behavior are unchanged.
- WP1 Ninety-Fourth Slice: the migrations adapter now resolves all store paths and the migration lock through the identity-preserving `sonder_runtime.platform.paths` seam; immutable migration replay and database locations are unchanged.
- WP1 Ninety-Fifth Slice: the packaged entrypoint now resolves its default backup target through `sonder_runtime.platform.paths`, preserving default-home resolution and explicit/configured target precedence.
- WP1 Eighty-Fifth Slice: moved the autopilot persistence database-path caller to `sonder_runtime.platform.paths` while preserving database and migration semantics.
- WP1 Ninety-Third Slice: packaged entrypoint build metadata now crosses the canonical platform version boundary; root release metadata remains available for tooling compatibility.
- WP1 Ninety-Sixth Slice: packaged web lifecycle build identity now crosses the canonical platform version boundary; lifecycle metrics and version payloads remain unchanged.
- WP1 Ninety-Seventh Slice: filesystem path implementation ownership now lives in `sonder_runtime.platform.paths`; the root `sonder_paths` module remains a thin identity-preserving compatibility alias with environment and legacy-migration behavior unchanged.
- WP1 Ninety-Eighth Slice: structured logging, redaction, and child-environment filtering now belong to `sonder_runtime.platform.logging`; `sonder_logging` remains a thin module-identity compatibility shim preserving logger and monkeypatch behavior.
- WP1 One-Hundred-Eleventh Slice: metrics ownership is now single-path under `sonder_runtime.platform.metrics`; the duplicate `sonder_metrics.py` root delegate is retired after all production callers and focused tests moved to the canonical module.
- WP1 One-Hundred-Twelfth Slice: unsafe-lab state now belongs to the security adapter, with pure explicit-input policy separated into the platform seam and zero architecture violations.
- WP1 One-Hundred-Twenty-Third Slice: durable operations-event sink ownership now belongs to `sonder_runtime.adapters.operations_event_sink`; the generic strangler name remains an identity-preserving compatibility alias.
- WP1 One-Hundred-Twenty-Fourth Slice: pure schema-gap formatting now belongs to `sonder_runtime.domain.schema_policy`, preserving the server compatibility alias.
- WP1 One-Hundred-Thirteenth Slice: autopilot repository ownership now belongs to `sonder_runtime.adapters.persistence.autopilot_repository`, removing the generic strangler repository implementation.
- WP1 One-Hundred-Fourteenth Slice: HTTP serve-temperature policy now belongs to `sonder_runtime.interfaces.http.serve_policy`, preserving the server compatibility alias.
- WP1 One-Hundred-Sixteenth Slice: process-probe ownership now belongs to `sonder_runtime.adapters.process_probe.ProcessProbeAdapter`; the generic strangler no longer owns that port adapter.
- WP1 Two-Hundred-Twenty-Fifth Slice: live-process fingerprint selection now belongs to `sonder_runtime.adapters.process_liveness.process_identity`; `ProcessProbeAdapter.identity` remains the compatibility port surface.
- WP1 One-Hundred-Fifteenth Slice: pure model-catalog capability normalization now belongs to `sonder_runtime.domain.model_capabilities`, preserving the server compatibility alias.
- WP1 One-Hundred-Eighteenth Slice: pure inline-thinking output policy now belongs to `sonder_runtime.domain.thinking_policy`, preserving the server compatibility alias.
- WP1 One-Hundredth Slice: full shutdown coordination now belongs to `sonder_runtime.platform.shutdown`; `sonder_shutdown` remains an identity-preserving compatibility shim with cancellation, signal, drain, deadline, and concurrent idempotence semantics unchanged.
- WP1 One-Hundred-Fifth Slice: process and dependency state now belong to `sonder_runtime.platform.service_state`; `sonder_service_state` remains an identity-preserving compatibility shim while lifecycle and shutdown consume the canonical implementation.
- WP1 One-Hundred-First Slice: build identity implementation now belongs to `sonder_runtime.platform.version`; `sonder_version.py` retains its literal release-tooling `VERSION` and identity-preserving compatibility surface.
- WP1 One-Hundred-Third Slice: the persistence migration registry now consumes build identity from `sonder_runtime.platform.version`; immutable migration bytes, checksums, replay, and release metadata remain unchanged.
- WP1 One-Hundred-Sixth Slice: full system-profile implementation ownership now lives in `sonder_runtime.platform.system_profile`; the root module remains an identity-preserving shim while hardware detection, mutable probe state, profile editing, and monkeypatch behavior remain unchanged.
- WP1 One-Hundred-Seventh Slice: the `sonder_version` root-platform allowance is removed after all packaged runtime callers moved to `sonder_runtime.platform.version`; the literal root `VERSION` contract remains intact for release tooling.
- WP1 One-Hundred-Eighth Slice: pure Ollama-origin normalization and fail-closed security policy now live in `sonder_runtime.domain.ollama_policy`; `unsafe_lab` no longer imports the transport adapter, preserving the security gate while removing the blocked platform-to-adapter dependency.
- WP1 Two-Hundred-Twenty-Sixth Slice: system-profile boolean environment overrides now use the canonical `sonder_runtime.platform.config_environment` policy, preserving the `_env_bool` compatibility alias and hardware override behavior.
- WP1 Two-Hundred-Twenty-Eighth Slice: launcher-health nonce, identity, and HMAC proof status policy now live in the packaged `sonder_runtime.domain.launcher_health` boundary, preserving the root `sonder_health` compatibility aliases.
- WP1 Two-Hundred-Forty-Sixth Slice: the remaining root doctor configuration-check policy now lives in the packaged `sonder_runtime.bootstrap.config_loading` boundary, preserving the root `_check_config` compatibility delegate.
- WP1 Two-Hundred-Thirtieth Slice: the bounded local-observability percentile helper now lives in `sonder_runtime.adapters.observability.latency_formatting`, preserving the `local_observability._percentile` compatibility alias and the root logging identity.
- WP1 Two-Hundred-Thirty-First Slice: headless argument parsing and command sequencing now live in `sonder_runtime.interfaces.cli.headless`, while `sonder_headless.py` preserves the supervisor implementation and compatibility surface.
- WP1 Two-Hundred-Thirty-Third Slice: unstamped build identity now reuses the packaged version commit probe, preserving the root `sonder_version` module identity and `_commit_from_git` compatibility helper.
- WP1 Two-Hundred-Thirty-Fourth Slice: cooperative cancellation now belongs to `sonder_runtime.platform.process`; packaged shutdown and root `sonder_shutdown` keep identity-preserving aliases.
- WP1 Two-Hundred-Thirty-Fifth Slice: speculative-tool safety policy now lives in the packaged `sonder_runtime.domain.speculation_policy` boundary, preserving the root `sonder_speculation.SPECULATABLE_TOOLS` alias and predictor seam.
- WP1 Two-Hundred-Forty-Third Slice: speculative-execution configuration helpers now live in the packaged `sonder_runtime.platform.speculation` boundary, preserving root helper aliases and the packaged domain safety policy.
- WP1 Two-Hundred-Thirty-Sixth Slice: the debug-dump export boundary now imports `Redactor` from canonical packaged logging, preserving the `debug_dump.Redactor` and root `sonder_logging` identities.
- WP1 Two-Hundred-Thirty-Seventh Slice: the lifecycle metric projection now belongs to `sonder_runtime.application.lifecycle`, preserving the web `_state_number` alias and root `sonder_service_state` identity.
- WP1 Two-Hundred-Forty-Second Slice: the remaining pure hardware sizing
  helpers now belong to the packaged domain boundary; accelerator and host
  platform probe ownership is documented without changing inventory or
  filesystem-text behavior.
- WP1 Two-Hundred-Forty-Fifth Slice: root hardware probe classification now
  uses identity-preserving aliases to the packaged platform boundary; the
  accelerator and host-platform probe seams remain explicitly packaged.
- WP1 Two-Hundred-Forty-Eighth Slice: standalone-client endpoint comparison and connection-error fallback now live in packaged client adapters; root names remain compatibility aliases.
- WP1 Two-Hundred-Forty-Ninth Slice: pure launcher output-tail, timeout, and operation-retention policy now live in `sonder_runtime.adapters.launcher_output`; root private helper names remain identity-preserving compatibility aliases.
- WP1 Two-Hundred-Fiftieth Slice: read-only memory-quality doctor policy now lives in `sonder_runtime.bootstrap.doctor_checks`, preserving the root `_check_memory_quality` compatibility delegate and injected legacy collaborators.
- WP1 Two-Hundred-Thirty-Eighth Slice: context-health text formatting now belongs to the packaged observability health-formatting boundary, preserving the generic packaged formatter alias while leaving the root launcher-health contract unchanged.
- WP1 Two-Hundred-Thirty-Ninth Slice: standalone-client HTTP execution now lives in `sonder_runtime.adapters.client_transport`, preserving the root `send_prompt` and `build_request` compatibility seams.
- WP1 Two-Hundred-Fortieth Slice: pure doctor terminal formatting and status rollup now live in `sonder_runtime.bootstrap.doctor_formatting`, preserving root rendering, status, and rollup aliases.
- WP1 Two-Hundred-Forty-First Slice: pure thinking-budget exhaustion detection now lives in `sonder_runtime.domain.thinking_policy`, preserving the root `server._thinking_exhausted_budget` alias.
- WP1 Two-Hundred-Forty-Seventh Slice: pure agent tool-invocation mutation policy now lives in `sonder_runtime.domain.agent_mutation_policy`, preserving the root mutation tool-set and predicate aliases.
- WP1 Two-Hundred-Forty-Fourth Slice: launcher idempotency-key normalization and durable replay validation now live in `sonder_runtime.adapters.launcher_idempotency`, preserving root helper and regex aliases.
- WP1 Two-Hundred-Twenty-Seventh Slice: environment-file parsing now belongs to the packaged `sonder_runtime.platform.config_environment` policy boundary, preserving the root `sonder_config.parse_env_file` and `ConfigError` contract.
- WP1 Two-Hundred-Twenty-Ninth Slice: packaged HTTP default-home and server-log resolution now use the canonical `sonder_runtime.platform.paths` boundary; the root `sonder_paths` identity alias remains compatible.
- WP1 Two-Hundred-Thirty-Second Slice: pure launcher lifecycle `context_size` normalization now belongs to `sonder_runtime.application.lifecycle`, preserving the root `sonder_launcher` helper and compatibility constants.
- WP1 One-Hundred-Second Slice: complete typed configuration ownership now belongs to `sonder_runtime.platform.config`; `sonder_config` remains a thin external-tooling compatibility surface with exact class, loader, exception, precedence, default, and validation semantics preserved.
# WP1 One-Hundred-Tenth Slice

- Extracted the state-independent runtime identity prompt renderer into
  `sonder_runtime.domain.runtime_identity`, reducing composition-root policy
  while preserving the `server._runtime_identity_block` compatibility surface.
- WP1 Two-Hundred-Ninety-Sixth Slice: pure fanout prompt-echo redaction now lives in `sonder_runtime.domain.fanout_redaction`, preserving the root `_fanout_redact_prompt_echo` alias.
- WP1 Two-Hundred-Ninety-Seventh Slice: pure agent decision parsing now lives in `sonder_runtime.domain.agents.decision_parsing`, preserving the root `_extract_agent_json` alias.
- WP1 Two-Hundred-Ninety-Eighth Slice: pure improvement report rendering now lives in `sonder_runtime.domain.improvement_report_formatting`, preserving the root `format_improvement_report` alias.
- WP1 Two-Hundred-Ninety-Ninth Slice: the pure natural-language model and fanout request grammar now lives in `sonder_runtime.domain.natural_model_request`, preserving the root `natural_model_request` and `_fanout_profile_scope` delegates and the selector constant aliases.
- WP1 Three-Hundredth Slice: the pure agent observation prompt framing (untrusted-data envelope, clipping and compaction) now lives in `sonder_runtime.domain.agents.observation_prompt`, preserving the root `_agent_observation_prompt` family aliases.
- WP1 Three-Hundred-First Slice: pure runtime source update rendering and its presentation-only eligibility verdict now live in `sonder_runtime.domain.updates.runtime_update_formatting`, preserving the root `_runtime_update_format` and `_runtime_update_eligibility` delegates.
- WP1 Three-Hundred-Second Slice: pure MCP runtime status rendering and the content-free refresh-error reducer now live in `sonder_runtime.domain.mcp_runtime_formatting`, preserving the root `format_mcp_runtime` delegate and the `_safe_mcp_error` alias.
- WP1 Three-Hundred-Third Slice: pure fanout receipt limits and the immutable admission record now live in `sonder_runtime.domain.fanout_admission`, preserving the root `_fanout_limits` alias and the `_fanout_admission` delegate.
- WP1 Three-Hundred-Fourth Slice: pure agent tool-name canonicalization now lives in `sonder_runtime.domain.agents.tool_naming`, preserving the root `_AGENT_TOOL_ALIASES` and `_canonical_agent_tool_name` aliases.
- WP1 Three-Hundred-Fifth Slice: the pure agent claim-review policy (negative-claim grammar, exact anchors, reviewer vocabulary and the exact-search action) now lives in `sonder_runtime.domain.agents.claim_review`, preserving the root constant and anchor aliases and the three hosted-policy delegates.
- WP1 Three-Hundred-Sixth Slice: pure agent evidence-quality checks and verifier-reach classification now live in `sonder_runtime.domain.agents.evidence_quality` and `sonder_runtime.domain.agents.verification_reach`, preserving the root `_ensemble_codegen_build_succeeded` and `_AGENT_VERIFICATION_TOOLS` aliases and the `_agent_tool_observation_ok` and `_agent_verifier_reachable` delegates.
- WP1 Three-Hundred-Seventh Slice: pure agent activity-command rendering (argv and batch-operation normalization plus the per-tool command line) now lives in `sonder_runtime.domain.agents.activity_command`, preserving the root `_agent_activity_command`, `_activity_argv`, `_agent_argv` and `_batch_agent_operations` aliases.
- WP1 Three-Hundred-Eighth Slice: pure ensemble synthesis prompts (candidate serialization, the untrusted-reference envelope, and the prose and code synthesis contracts) now live in `sonder_runtime.domain.ensemble_synthesis`, preserving the root `_ensemble_*` aliases.
- WP1 Three-Hundred-Ninth Slice: pure context-pack argument normalization (paths, bounded integers and the UTF-8 byte prefix) now lives in `sonder_runtime.domain.context.pack_arguments`, preserving the root `_context_pack_*` aliases.
- WP1 Three-Hundred-Tenth Slice: pure local tier model-binding checks (installed-tag matching and the catalog capability mismatch) now live in `sonder_runtime.domain.runtime_model_binding`, preserving the root `_runtime_model_is_installed` and `_runtime_model_capability_error` aliases.
- WP1 Three-Hundred-Eleventh Slice: pure runtime recovery-stash rendering now lives in `sonder_runtime.domain.updates.stash_formatting`, preserving the root `_runtime_stash_format` alias.
- WP1 Three-Hundred-Twelfth Slice: pure hosted and local thinking controls (per-model hosted policy, the think=false allow-list, the think-option refusal recognizer and the local thinking budget) now live in `sonder_runtime.domain.thinking_controls`, preserving the root aliases and the `_apply_cloud_thinking_policy` delegate.
- WP1 Three-Hundred-Thirteenth Slice: pure fanout receipt safety (the credential scrubber over redacted answers and the immutable target-snapshot check) now lives in `sonder_runtime.domain.fanout_receipts`, preserving the root `_fanout_safe_answer` alias and the `_fanout_snapshot_allows` delegate.
- WP1 Three-Hundred-Fourteenth Slice: the pure empty-model-response description now lives in `sonder_runtime.domain.model_response_detail`, preserving the root `_empty_model_response_detail` alias.
- WP1 Three-Hundred-Fifteenth Slice: pure loop action resolution (the non-tool action table, the tool that actually runs, and the success-prefix verdict) now lives in `sonder_runtime.domain.loop_actions`, preserving the root `_LOOP_ACTION_TOOLS` and `_loop_action_tool` aliases and the `_loop_verdict_result` delegate.
- WP1 Three-Hundred-Sixteenth Slice: pure serve-target selection policy (explicit selection and the cloud availability-fallback rule) now lives in `sonder_runtime.domain.serve_selection`, preserving the root `_allow_cloud_fallback_for_target` and `_explicit_serve_selection` aliases.
- WP1 Three-Hundred-Seventeenth Slice: pure context compaction plan rendering now lives in `sonder_runtime.domain.context.compaction_plan_formatting`, preserving the root `format_context_compaction_plan` alias.
- WP1 Three-Hundred-Eighteenth Slice: fanout transport-failure classification and safe receipt rendering now live in `sonder_runtime.adapters.fanout_failures`, preserving the root `_fanout_failure_class`, `_fanout_safe_error` and `_fanout_no_eligible_models_error` aliases.
- WP1 Three-Hundred-Nineteenth Slice: the model-call contracts for empty-response metadata and the offload schema argument now live in `sonder_runtime.adapters.model_response_metadata` and `sonder_runtime.adapters.offload_schema_argument`, preserving the root `_response_error_metadata` and `_parse_schema_arg` aliases.
- WP1 Three-Hundred-Twentieth Slice: stable agent call signatures for de-duplicating equivalent tool calls now live in `sonder_runtime.adapters.agent_call_signature`, preserving the root `_agent_call_signature` delegate.
- WP1 Three-Hundred-Twenty-First Slice: the pure campaign task prompt now lives in `sonder_runtime.domain.campaign_prompt`, preserving the root `_campaign_prompt` delegate.
- WP1 Three-Hundred-Twenty-Second Slice: pure autopilot command program extraction now lives in `sonder_runtime.domain.automation.command_programs`, preserving the root `_autopilot_command_programs` alias.
- WP1 Three-Hundred-Twenty-Third Slice: agent decision generation with bounded format repair now lives in `sonder_runtime.adapters.agent_decision_generation`, preserving the root `_AGENT_DECISION_REPAIR_LIMIT` alias and the `_agent_generate_decision` delegate.
- WP1 Three-Hundred-Twenty-Fourth Slice: hard-bounded hosted agent generation and its per-call and total ceilings now live in `sonder_runtime.adapters.bounded_cloud_generation`, preserving the root `_bounded_cloud_agent_generate`, `_CLOUD_AGENT_NUM_PREDICT` and `_CLOUD_AGENT_OUTPUT_BUDGET` aliases.
- WP1 Three-Hundred-Twenty-Fifth Slice: advisory fanout model-health recording and cooldowns now live in `sonder_runtime.adapters.fanout_health`, preserving the root `_fanout_health` delegate.
- WP1 Three-Hundred-Twenty-Sixth Slice: serializable fanout receipts now live in `sonder_runtime.adapters.fanout_receipt`, preserving the root `_fanout_receipt` delegate.
- WP1 Three-Hundred-Twenty-Seventh Slice: the agent work-coverage family (mutation records, path containment, the no-op flag and build-driver tables, and the validation and verification coverage predicates) now lives in `sonder_runtime.adapters.agent_work_coverage`, preserving every root alias.
- WP1 Three-Hundred-Twenty-Eighth Slice: the bounded repo-repair pytest runner now lives in `sonder_runtime.adapters.repo_repair_runner`, preserving the root `_repo_repair_pytest` alias.
- WP1 Three-Hundred-Twenty-Ninth Slice: pure Ollama catalog parsing (names, records, installed snapshot, tag revision and exact resolution) now lives in `sonder_runtime.domain.model_catalog`; the root discovery functions keep the fetch and every monkeypatch seam as thin delegates.
- WP1 Three-Hundred-Thirtieth Slice: the hosted K3-to-K2.7 availability fallback now lives in `sonder_runtime.adapters.cloud_fallback`, preserving the root `_chat_request_with_cloud_fallback` and `_cloud_extra_usage_fallback` delegates.
- WP1 Three-Hundred-Thirty-First Slice: the pure fanout no-load residency fence now lives in `sonder_runtime.domain.fanout_residency`, preserving the root `_fanout_dispatch_residency_reason` delegate.
- WP1 Three-Hundred-Thirty-Second Slice: database-backed session turn claims now live in `sonder_runtime.adapters.session_turn_claims`, preserving the root `_acquire_persistent_session_turn` and `_release_persistent_session_turn` delegates.
- WP1 Three-Hundred-Thirty-Third Slice: compare-and-swap persistence of a verified code repair now lives in `sonder_runtime.adapters.code_repair_persistence`, preserving the root `_persist_verified_code_repair` delegate.
- WP1 Three-Hundred-Thirty-Fourth Slice: project-scoped path key lookup now lives in sonder_runtime.domain.project_scope_keys, preserving the root _project_scoped_path_key compatibility alias.
- WP1 Three-Hundred-Thirty-Fifth Slice: selfmod test command construction now lives in sonder_runtime.domain.automation.selfmod_test_commands, preserving the root _selfmod_test_commands compatibility alias.
- WP1 Three-Hundred-Thirty-Sixth Slice: approval listing limit parsing now lives in sonder_runtime.domain.approvals_limit, preserving the root _approvals_limit compatibility alias.
- WP1 Three-Hundred-Thirty-Seventh Slice: callable keyword inspection now lives in sonder_runtime.domain.callable_inspection, preserving the root _callable_accepts_keyword compatibility alias.
- WP1 Three-Hundred-Thirty-Eighth Slice: fanout worker identity now lives in sonder_runtime.domain.fanout_worker_identity, preserving the root _FANOUT_WORKER_INSTANCE alias and _fanout_worker_id compatibility delegate.
- WP1 Three-Hundred-Thirty-Ninth Slice: runtime policy update JSON parsing now lives in sonder_runtime.domain.runtime_update_parsing, preserving the root _runtime_update_object alias.
- WP1 Three-Hundred-Fortieth Slice: execution-decision summary header formatting now lives in sonder_runtime.domain.execution_route_formatting, preserving the root _execution_route_header compatibility delegate.
- WP1 Three-Hundred-Forty-First Slice: agent tool observation quality classification now lives in sonder_runtime.domain.agent_observation_quality, preserving the root _agent_observation_ok alias.
- WP1 Three-Hundred-Forty-Second Slice: agent run-created-paths key normalization now lives in sonder_runtime.domain.agent_path_keys, preserving the root _agent_created_path_key alias.
- WP1 Three-Hundred-Forty-Third Slice: agent model-escalation identity key now lives in sonder_runtime.domain.agent_escalation_identity, preserving the root _agent_escalation_key alias.
- WP1 Three-Hundred-Forty-Fourth Slice: cloud tier classification now lives in sonder_runtime.domain.model_routing.is_cloud_tier, preserving the root _is_cloud_tier compatibility delegate.
- WP1 Three-Hundred-Forty-Fifth Slice: fanout worker id construction now delegates to sonder_runtime.domain.fanout_worker_identity.fanout_worker_id, removing duplicated logic from the root _fanout_worker_id delegate.
- WP1 Three-Hundred-Forty-Sixth Slice: agent help text tool-name parsing now lives in sonder_runtime.domain.agent_help_parsing, preserving the root _agent_help_advertised_tools alias.
- WP1 Three-Hundred-Forty-Seventh Slice: agent mutation record convenience wrapper now lives in sonder_runtime.adapters.agent_work_coverage.mutation_record, preserving the root _agent_mutation_record alias.
- WP1 Three-Hundred-Forty-Eighth Slice: loop result formatting now lives in sonder_runtime.domain.loop_result_formatting, preserving the root _loop_text_result identity-preserving alias.
- WP1 Three-Hundred-Forty-Ninth Slice: identifier resolution now lives in sonder_runtime.domain.identifier_resolution, preserving the root _resolve_session and _resolve_project compatibility delegates.
- WP1 Three-Hundred-Fiftieth Slice: cloud agent tool policy now lives in sonder_runtime.domain.cloud_agent_tool_policy, preserving the root _cloud_agent_tool_policy_error compatibility delegate.
- WP1 Three-Hundred-Fifty-First Slice: schema coverage annotation now lives in sonder_runtime.domain.schema_policy.with_schema_coverage, preserving the root _with_schema_coverage identity-preserving alias.
