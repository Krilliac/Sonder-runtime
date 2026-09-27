"""The human-readable MCP tool catalog ``tool_manifest`` serves.

One slash-separated key per tool family; ``tests/test_advertised_surface_drift.py``
checks every name advertised here against the tools the MCP server registers.
"""
from __future__ import annotations

MCP_TOOL_MANIFEST = {
    "agent": "Run a Claude-like tool-calling loop that can use local tools and web tools. Exact-ack unsafe lab mode removes its host tool policy only on a loopback, unprivileged process.",
    "autopilot_start/autopilot_status/autopilot_resume/autopilot_pause/autopilot_cancel": "Run a restart-persistent local goal with evidence-aware checkpoints, bounded replans, host tool gates, and explicit lifecycle control.",
    "runtime_policy_status/runtime_policy_update": "Inspect or guarded-edit shared hot-reloadable local model mappings and execution-lane tiers; cloud opt-in stays separate.",
    "cloud_opt_in": "Show, explicitly enable, or immediately revoke process-local hosted/cloud consent; enabling allows later cloud-* prompts to leave the machine and does not persist across restart.",
    "runtime_source_update_status/runtime_source_update": "Check the installed Git commit and canonical origin/main update time, or safely fast-forward only a clean canonical Sonder source checkout. Updates never merge/rebase/overwrite local work and require restart.",
    "mcp_runtime_status/live_reload_status": "Audit atomic MCP source/tool convergence, refresh history, list-change signaling, and fail-closed reload errors.",
    "master_orchestrate/master_status/master_capacity/master_cancel/master_retry": "Run restart-safe hardware-scheduled orchestration, inspect capacity/activity, cancel fleets, and explicitly retry interrupted work.",
    "admin_register/admin_login/admin_accounts/admin_set_account": "Manage hosted accounts, roles, bans, tiers, and developer flags.",
    "admin_status/debug_inspect/admin_private_chain_of_thought": "Inspect admin/debug state; private chain-of-thought is refused unless the operator opted in twice (SONDER_ALLOW_PRIVATE_COT plus an explicit allow rule), and then serves only the reasoning record reasoning_show serves.",
    "sonder": "Ask through Sonder Runtime's local learning loop.",
    "offload": "Route a self-contained task to a configured local/cloud tier.",
    "model_fanout/model_fanout_recent/model_fanout_status/model_fanout_cancel/model_fanout_resume/model_fanout_synthesize": "Run a durable, bounded fanout across discovered local, cloud, or all chat models; list caller-scoped safe recent-run summaries after a restart, inspect its owner-scoped receipt, cancel it, explicitly retry finished results, or locally synthesize one completed receipt's exact complete answer previews. Synthesis has no natural-language route, requires two non-truncated answered receipts and a fixed/discovered local generative model, and persists neither synthesis nor reasoning. Fixed profiles are `healthy-local-chat`, `healthy-cloud-chat`, `healthy-chat`, and `loaded-local-chat`; they exclude non-chat targets and active health cooldowns, but never accept arbitrary selectors. `loaded-local-chat` is local-only and fails closed unless Ollama confirms residency at both planning and dispatch, so it never triggers a model load. Natural chat supports `use code and reasoning ensemble to review ...` for a fixed local two-tier answer, `ask all healthy local chat models: ...`, `ask all loaded local chat models: ...`, `ask all available models for ...`, `ask all available local models: ...`, `ask all local and cloud models: ...`, `ask all local models and cloud models: ...`, `ask all Sonder models + cloud: ...`, `run every available cloud models to answer: ...`, `run phi4:latest to ...`, `ask the phi4:latest model to ...`, `run using model phi4:latest: ...`, `run using phi4:latest: ...`, `run using phi4:latest to ...`, and `ask with qwen2.5-coder:14b for ...`. Compiler-feedback repair remains the explicit `codegen_build_loop` tool because it needs an approved project root, an exact file contract, and an exact build command; it is never inferred from conversational text. Cloud use still needs explicit operator opt-in; shared deployments restrict fanout and ensembles to developer-authorized callers.",
    "web_search/web_fetch/weather_lookup/approximate_location_lookup": "Search/fetch public pages, get sourced weather, or resolve an explicitly consented approximate IP location without retaining the IP.",
    "local_service_probe": "Bounded unauthenticated GET/HEAD health probe for an explicit-port HTTP/HTTPS service resolving exclusively to loopback.",
    "workspace_inventory/workspace_compare/dependency_inventory/directory_tree/directory_create/text_search/file_read_range/context_pack": "Budgeted guarded workspace/dependency inventory and metadata-only comparison, folder discovery, creation, text search, bounded line-range reads, and multi-file context packs.",
    "repo_status/repo_diff": "Inspect bounded read-only Git branch, worktree, staged, and unstaged state without shell execution.",
    "project_detect": "Inventory guarded build/test/runtime manifests and return deterministic evidence-backed language, framework, and cross-platform argv candidates without executing them.",
    "file_policy/file_find/file_read/file_write/file_batch_write/json_patch/file_edit/file_copy/file_move/file_delete/text_patch": "Guarded filesystem find/read/create/edit/transactional batch write/atomic JSON patch/single-file transfer/delete and strict unified-diff preview/apply.",
    "repository_symbol_index": "Build a deterministic bounded read-only declaration index with Python AST and conservative JS/TS/C/C++/C#/Rust/Go extraction.",
    "repo_log/repo_show/repo_blame": "Read bounded structured Git history, patches, and line attribution from an exact project repository without shell execution or upward discovery.",
    "file_digest/directory_digest": "Stream guarded files into SHA-256 and build deterministic relative-path manifests with fail-closed complete or explicitly partial directory Merkle roots.",
    "archive_list/archive_extract": "Prevalidate bounded ZIP/TAR manifests or transactionally extract them to a new non-overwriting workspace directory.",
    "archive_create": "Transactionally create a bounded deterministic ZIP/TAR from explicit guarded project inputs without overwriting.",
    "artifact_risk_inspect": "Statically inspect guarded PDFs, PE/ELF/Mach-O executables, scripts, or opaque binaries for bounded risk indicators without executing or returning content.",
    "process_list/process_memory_risk_inspect": "Opt-in bounded Windows process metadata and fixed-indicator memory-risk inspection; never returns command lines, paths, addresses, strings, or raw bytes.",
    "log_inspect": "Inspect one guarded text log with fixed level/timestamp/source extraction, failure clusters, repeats, and bounded context.",
    "scaffold_project": "Write a complete deterministic project skeleton (cpp-msvc .sln/.vcxproj, cpp-cmake, csharp, rust, python, node, typescript, go, java-maven) -- never hand-write solution/build plumbing.",
    "environment_status": "Report the host OS, available shells (PowerShell/cmd/bash/wsl), and installed toolchains -- check before choosing a command shape or assuming a tool exists.",
    "toolchain_status": "Run one fixed, bounded, local version probe for a tool already discovered by environment_status; it never accepts a command or arguments.",
    "tool_inventory": "Report the categorized host tool inventory (compilers, build systems, test runners, linters, package managers, runtimes, containers, VCS, cloud CLIs, editors) with redacted paths and cached fixed-probe versions.",
    "output_digest": "Summarize a guarded log file or your own test-run job output: final line, run summary counts, FAILED/ERROR lines, first parsed compiler/test errors, and a short tail.",
    "hardware_profile": "Detect cross-vendor accelerators and report conservative resident, unified-memory, and GPU+RAM-spill model plans without changing host settings.",
    "data_inspect/data_query/sqlite_mutate": "Preview structured data, run bounded read-only queries, or explicitly preview/apply one guarded parameterized SQLite DML statement.",
    "data_convert": "Preview or atomically create a non-overwriting JSON/JSONL/CSV/TSV conversion with explicit ordered fields.",
    "program_search/script_search/workspace_run/script_run/image_inspect": "Discover installed programs and workspace scripts, run bounded argv-only processes, and inspect image metadata; script_run applies the operator execution-risk policy before launch.",
    "task_create/task_list/task_update/task_show/task_delete/task_plan/task_progress/task_ledger/task_depend/checklist_create/checklist_update/checklist_show": "Visible todo, ordered checklist, and digest-bound manager ledger state shared by console, app, agents, and MCP. task_plan batch-creates a work plan with ordered steps and auto-dependencies. task_progress shows a compact summary; task_ledger exposes bounded dependencies and replan metadata.",
    "workbench_agent": "Run an autonomous local tool loop with a guaranteed checklist, exact action transcript, validation gate, and end report.",
    "command_registry_list": "Inspect available slash commands by category, name, or risk.",
    "tool_manifest/tool_capability_manifest/access_request_preview": "Inspect the human-readable MCP tool catalog, fingerprint the live registered capability schemas, or preview a non-authorizing scoped filesystem access request.",
    "activity_status": "Inspect active/latest response activity, tool calls, and file changes.",
    "permission_policy/permission_rule_set/permission_approve/permission_approvals": "Inspect the effective permission decision -- the rule, the active mode, and which one governs -- guarded-edit a rule, or approve exactly one refused call once and list what asked.",
    "context_compaction_plan": "Preview when to summarize, split sessions, or reduce live context.",
    "run_code": "Run a bounded snippet: Python, JS/TypeScript, Bash, Ruby, Perl, PHP, Lua, R, Go, Java, Rust, PowerShell, C++, C#.",
    "isolated_run": "Direct MCP-only, explicitly enabled and developer-authorized Docker/Podman execution with approved roots, separate writable approval, and a fixed resource-capped isolation policy.",
    "ground_artifact": "Validate in-memory non-code content with exact/contains/regex/JSON checks.",
    "artifact_ground": "Validate files or bundles with inferred writing, data, editable Office/media/timelines, UI, image, audio, and static or animated humanoid model recipes.",
    "run_project": "Run a bounded temporary multi-file project with optional build commands.",
    "artifact_generate/artifact_verify": "Create and verify stdlib-only images, animated GIF/AVI video, SVGs, Office files, MIDI/WAV audio, captions, EDL timelines, data, web mockups, OBJ and textured humanoid GLBs with full morph frames and clip sequences, scenes, and themed packs from a free-form brief.",
    "game_reference_suite/game_generate_and_test/game_generation_campaign": "Build, execute, repair, and ground persistent in-house 2D/2.5D/3D game projects and fleets.",
    "loop": "Repeat bounded code/model/system actions.",
    # Spell every tool out.  The old "workflow_list/save/run/delete"
    # shorthand read as four tool names, three of which ("save", "run",
    # "delete") are not registered tools at all -- the only names on any
    # advertising surface that no @mcp.tool() backs.
    "workflow_list/workflow_save/workflow_run/workflow_delete": "Manage reusable loop workflows.",
    "system_profile_text/update_system_profile": "Read or edit standing instructions.",
    "emotion_vector_status/update_emotion_vectors/tune_emotion_vectors": "Read, edit, or live-tune tone vectors.",
    "learn_preference/preferences_status": "Read or teach durable user behavior/workflow preferences.",
    "memory_search/memory_export/session_export": "Inspect local memory.",
    "learning_health_status": "Inspect grounded outcome coverage, signal quality, lesson provenance, distillation yield, and memory hygiene.",
    "evaluation_history_status": "Read explicit evaluation trends separated by exact model digest and suite version/digest; it never runs or promotes a model.",
    "memory_quality_report/memory_quality_repair": "Audit and dry-run/prune exact duplicate lessons.",
    "memory_privacy_review/memory_privacy_repair": "Review redacted privacy findings and explicitly dry-run/remove selected flagged lessons.",
    "memory_embedding_backfill": "Dry-run or refresh stale/missing semantic vectors with the local embedding model.",
    "memory_interaction_embedding_backfill": "Dry-run or locally refresh stale raw-interaction task vectors without printing task text.",
    "system_improvement_report": "Suggest next improvements from learning, memory, context, and deployment signals.",
    "context_policy_status/set_context_size": "Show or select requested virtual context up to 1m while clamping Ollama native num_ctx.",
    "learn_from_example/apply_learned": "Teach from examples and preview lesson application.",
    "self_heal_check/self_heal_repair": "Detect and safely repair common local breakage.",
    "context_health/diagnostics/live_reload_status/status/unload": "Observe and manage runtime health.",
    "record_outcome": "Feed grounded outcomes back into learning.",
    "sonder_stats/sonder_sessions/sonder_remember_fact/sonder_forget_fact": "Memory observability and durable facts.",
}


def render_tool_manifest() -> str:
    """The catalog as sorted ``  name: purpose`` lines."""
    return "\n".join("  %s: %s" % item for item in sorted(MCP_TOOL_MANIFEST.items()))
