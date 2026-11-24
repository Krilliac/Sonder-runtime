"""Static agent tool help catalogs, policy tool sets, and pure prompt builders."""
from __future__ import annotations

from collections.abc import Sequence

AGENT_TOOL_HELP: str = """Available tools:
- tool_help: {"query": "search"}
- file_check: {"path": "file.py"}
- agent_lane: {"action": "spawn|list|inspect|send_message|wait|interrupt|resume|cancel|reports|ack|retrieve_archive", "payload": {}} -- parent authority is inherited from this run. Spawn payload: command_id, task, workspace_root (within the configured project grant), optional title/tier/max_steps/max_output_tokens/max_wall_seconds. Other actions use lane_id; send_message uses command_id/content; controls use command_id; ack uses report_id/command_id; retrieve_archive uses lane_id/archive_id. Never supply parent identity or tokens.
- run_code: {"code": "...", "language": "python|js|powershell|cpp|csharp", "stdin": "", "timeout": 10} -- source snippet only; never pass a shell command such as `cargo --version`
- run_project: {"files_json": {"files": {"src/main.cpp": "..."}}, "commands_json": [{"cmd": ["g++", "src/main.cpp", "-o", "app"]}], "stdin": "", "timeout": 60}
- artifact_generate: {"name": "brand-kit", "brief": "fiery logo, DOCX report, AVI video, MIDI score, captions, textured humanoid 3D mascot with full morph frames and sequenced Idle Walk Run clips", "kinds": "auto|all|icon,vector,diagram,document,docx,data,spreadsheet,presentation,animation,video,music,midi,captions,timeline,web,model,rigged_model", "dimension": "auto|2d|2.5d|3d", "theme": "auto|ember|verdant|arcane|frost"}
- artifact_verify: {"path": "artifacts/generated/brand-kit"}
- artifact_ground: {"path": "artifacts/generated/brand-kit", "recipe": "auto|bundle|writing|data|office|docx|xlsx|pptx|avi|gif|glb|midi|srt|vtt|edl|ui|markdown|json|csv|html|svg|png|ppm|wav|obj", "requirements_json": {"required_files": ["rigged.glb"], "min_vertices": 384, "min_triangles": 192, "min_joints": 17, "min_animations": 6, "min_animation_sequences": 2, "min_skeletal_animations": 4, "min_morph_animations": 2, "min_morph_targets": 2, "min_images": 3, "min_textures": 3, "min_texcoord_sets": 1, "required_animation_clips": ["Idle", "Walk", "Run", "Breathe", "Focus"], "require_humanoid_rig": true, "require_animation_clip_metadata": true, "require_morph_normals": true, "require_morph_tangents": true, "require_embedded_images": true, "require_material_textures": true, "require_named_animations": true, "require_named_morph_targets": true, "require_power_of_two_images": true, "require_tangents": true, "no_external_dependencies": true}}
- game_reference_suite: {"name": "reference-suite", "theme": "arcane", "max_workers": 2, "timeout": 30}
- game_generate_and_test: {"name": "arena", "concept": "isometric action RPG", "language": "python|javascript|cpp|csharp", "dimension": "2d|2.5d|3d", "theme": "arcane", "repair_rounds": 1}
- game_generation_campaign: {"name": "game-fleet", "concept": "action roguelite", "total": 6, "language": "", "dimension": "", "theme": "arcane", "max_workers": 2, "repair_rounds": 1}
- web_search: {"query": "...", "limit": 5}
- web_fetch: {"url": "https://...", "max_chars": 8000}
- weather_lookup: {"location": "Chicago, IL|60601", "forecast_days": 3, "units": "auto|metric|imperial"}
- approximate_location_lookup: {"consent": true} (only after the user explicitly enables or requests IP location)
- file_policy: {}
- workspace_inventory: {"path": ".", "max_entries": 20000, "timeout_seconds": 10, "top_n": 15}
- workspace_compare: {"left": "path/to/left", "right": "path/to/right", "max_entries": 2000, "max_file_bytes": 64000000, "max_total_bytes": 256000000, "max_details": 1000, "max_output_bytes": 256000, "timeout": 5}
- repo_status: {"root": ".", "timeout": 10, "max_output": 128000}
- repo_diff: {"root": ".", "staged": false, "path": "", "context": 3, "timeout": 10, "max_output": 128000}
- project_detect: {"path": ".", "max_depth": 8, "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_results": 500}
- dependency_inventory: {"path": ".", "max_depth": 5, "max_files": 100, "max_total_bytes": 2000000, "max_results": 2000}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- directory_create: {"path": "output/reports", "parents": true}
- file_find: {"query": "*.py", "root": ".", "max_results": 50}
- repository_symbol_index: {"path": ".", "glob": "*", "language": "auto|python|javascript|typescript|c|cpp|csharp|rust|go", "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_symbols": 2000}
- file_read: {"path": "README.md", "offset": 1, "limit": 120}
- file_digest: {"path": "artifact.bin", "max_bytes": 32000000}
- directory_digest: {"path": ".", "max_depth": 12, "max_files": 2000, "max_total_bytes": 32000000, "max_file_bytes": 32000000, "max_results": 2500}
- file_read_range: {"path": "server.py", "start_line": 1, "end_line": 200}
- context_pack: {"paths_json": ["README.md", "src/main.py"], "max_files": 12, "max_total_bytes": 256000, "max_bytes_per_file": 64000}
- repo_log: {"path": ".", "revision": "HEAD", "file_path": "", "count": 20, "timeout": 5, "max_bytes": 256000}
- repo_show: {"path": ".", "revision": "HEAD", "file_path": "<required contained relative file>", "timeout": 5, "max_bytes": 256000}
- repo_blame: {"path": ".", "file_path": "<contained relative file>", "revision": "HEAD", "start_line": 1, "end_line": 100, "timeout": 5, "max_bytes": 256000}
- archive_list: {"path": "bundle.zip", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100, "max_results": 2500}
- archive_extract: {"source": "bundle.zip", "destination": "unpacked", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100} -- creates a new directory; never overwrites
- archive_create: {"root": ".", "inputs_json": ["src", "README.md"], "destination": "release.zip", "archive_format": "zip|tar", "deterministic": true} -- destination must be new and outside input directories
- artifact_risk_inspect: {"path": "artifact.exe", "max_scan_bytes": 16777216, "max_seconds": 5} -- static indicators only; no content execution or raw content return
- process_list: {"max_processes": 128, "max_seconds": 0.5} -- requires exact host opt-in; names/PIDs/counts only
- process_memory_risk_inspect: {"pid": 1234, "max_bytes": 4194304, "max_regions": 256, "max_seconds": 1} -- fixed aggregate indicators only; never raw memory
- log_inspect: {"path": "logs/app.log", "tail_lines": 0, "context_lines": 2, "max_file_bytes": 64000000, "max_scan_bytes": 4000000, "max_lines": 10000, "max_line_bytes": 4096, "max_results": 100, "max_output_bytes": 256000, "timeout": 5}
- text_search: {"query": "TODO", "root": ".", "glob": "*.py", "regex": false, "max_results": 100}
- file_write: {"path": "notes.txt", "content": "...", "mode": "create|overwrite|append"}
- file_batch_write: {"operations_json": [{"path": "a.txt", "content": "...", "mode": "create|overwrite"}]}
- json_patch: {"path": "config.json", "operations_json": [{"op": "test", "path": "/version", "value": 1}, {"op": "replace", "path": "/version", "value": 2}], "mode": "preview|apply"}
- text_patch: {"root": ".", "patch": "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n", "apply": false}
- file_edit: {"path": "notes.txt", "old": "before", "new": "after", "count": 1} -- must match exactly once; whitespace and CRLF tolerant; result echoes the region with line numbers
- file_copy: {"source": "assets/input.bin", "destination": "build/input.bin", "overwrite": false}
- file_move: {"source": "build/draft.bin", "destination": "dist/final.bin", "overwrite": false}
- file_delete: {"path": "notes.txt", "dry_run": true}
- scaffold_project: {"kind": "cpp-msvc|cpp-cmake|csharp|rust|python|node|typescript|go|java-maven", "name": "MyApp", "root": "MyApp"} -- writes the full skeleton (.sln/.vcxproj/Cargo.toml/...); use this instead of hand-writing build/solution files
- environment_status: {} -- host OS, shells, installed toolchains; check before choosing command shapes
- toolchain_status: {"name": "cargo|git|cmake|...", "refresh": false} -- fixed, local-only version probe for a discovered tool; no command or arguments
- tool_inventory: {"category": "compiler|build_system|test_runner|linter_formatter|...", "name": "", "refresh": false} -- categorized installed-tool inventory with versions; paths redacted
- output_digest: {"path": "<task-relevant log file>", "job_id": "", "tail_lines": 20} -- pass exactly one of path or job_id; tail -1 + FAILED/ERROR lines + first parsed errors
- hardware_profile: {"workload": "general|chat|coding|agentic|research", "refresh": false} -- cross-vendor device inventory and conservative local-model fit; detection is not backend readiness
- script_search: {"query": "build", "root": ".", "max_results": 100}
- program_search: {"query": "python", "max_results": 50}
- workspace_run: {"program": "git", "args_json": ["status", "--short"], "cwd": ".", "timeout": 30}
- script_run: {"path": "scripts/check.py", "args_json": [], "cwd": ".", "timeout": 30, "risk_policy": "off|report|deny-high|deny-medium|deny-unknown"} -- request may strengthen but never weaken operator policy
- test_discover: {"root": ".", "framework": "auto"} -- discover tests; auto-detects pytest/jest/vitest/cargo/go/dotnet
- test_run: {"root": ".", "framework": "auto", "path": "", "pattern": "", "verbose": false, "coverage": false, "timeout": 120} -- run tests filtered by path or pattern; pytest uses the structured runner (host-owned command, hard deadline)
- lint_run: {"root": ".", "tool": "auto", "path": "", "fix": false, "timeout": 60} -- lint with ruff/flake8/eslint/clippy; fix=true to auto-fix
- format_code: {"root": ".", "tool": "auto", "path": "", "check_only": false, "timeout": 60} -- format with ruff/black/prettier/rustfmt/gofmt
- typecheck_run: {"root": ".", "tool": "auto", "path": "", "timeout": 120} -- type check with mypy/pyright/tsc
- dependency_add: {"root": ".", "packages_json": "[\"requests\"]", "dev": false, "timeout": 60} -- install packages with auto-detected manager
- dependency_remove: {"root": ".", "packages_json": "[\"requests\"]", "timeout": 60} -- uninstall packages
- dependency_update: {"root": ".", "packages_json": "[]", "timeout": 120} -- update packages (empty = all)
- dependency_audit: {"root": ".", "timeout": 60} -- audit for vulnerabilities
- git_commit: {"root": ".", "message": "fix: description", "paths_json": "[\"src/main.py\"]", "all_tracked": false, "timeout": 30} -- commit staged or specified files
- git_branch: {"root": ".", "name": "feature/x", "checkout": true, "base": "", "timeout": 10} -- create and checkout a branch
- git_checkout: {"root": ".", "ref": "main", "timeout": 10} -- switch branch/tag/commit
- git_stash: {"root": ".", "action": "push|pop|list|drop", "message": "", "include_untracked": true, "timeout": 10}
- git_tag: {"root": ".", "name": "v1.0.0", "message": "", "delete": false, "timeout": 10}
- git_merge: {"root": ".", "branch": "feature/x", "no_ff": true, "message": "", "timeout": 30}
- git_cherry_pick: {"root": ".", "commits_json": "[\"abc123\"]", "timeout": 30}
- build_run: {"root": ".", "command": "", "timeout": 120} -- auto-detects Make/Cargo/CMake/Meson/.NET/Flutter-Dart/Go/npm/Gradle/Maven; command overrides
- ensemble_codegen_build_loop: {"project_dir": ".", "files_json": "{\"src/main.py\": \"task-specific file contract\"}", "build_program": "python", "build_args_json": "[\"-m\", \"pytest\", \"-q\"]", "timeout": 120} -- local code + reasoning ensemble with two compiler-feedback attempts per file; requires a project
- build_clean: {"root": ".", "timeout": 30} -- clean build artifacts
- rename_symbol: {"root": ".", "old_name": "foo", "new_name": "bar", "glob": "**/*.py", "dry_run": true} -- rename across files; dry_run=false to apply
- find_references: {"root": ".", "symbol": "MyClass", "glob": "**/*.py"} -- find all occurrences of a symbol
- diff_files: {"root": ".", "left": "a.py", "right": "b.py", "context": 3} -- unified diff between two files
- apply_patch: {"root": ".", "patch_text": "...", "check_only": false} -- apply a unified diff patch
- secret_scan: {"root": ".", "timeout": 30} -- scan for leaked API keys, passwords, tokens, private keys
- image_inspect: {"path": "artifacts/generated/demo/icon.png"}
- document_extract: {"path": "artifacts/generated/demo/document.docx", "max_chars": 32000, "max_items": 200} -- extract text/tables/slides/cues from DOCX, XLSX, PPTX, HTML, SVG, SRT, VTT
- media_inspect: {"path": "artifacts/generated/demo/rigged.glb"} -- inspect WAV audio, MIDI, AVI video, GLB/glTF/OBJ/STL 3D meshes
- code_metrics: {"path": ".", "max_files": 2000, "top_n": 10} -- language LOC, TODO/FIXME markers, largest files, and complexity hotspots
- schema_validate: {"path": "config.json", "schema_path": "config.schema.json", "schema_json": ""} -- validate JSON/JSONL/TOML against JSON Schema
- language_matrix: {"language": ""} -- report supported languages, tools, and live host toolchain readiness
- math_eval: {"expression": "comb(52,5)", "action": "eval|solve|matrix|convert|date|derivative|integrate", "exact": false} -- exact/high-precision math, equations, matrices, statistics, units, dates
- logic_verify: {"formula": "(A -> B) & A", "action": "satisfiable|truth_table|equivalent|implies|cnf|csp", "formula_b": ""} -- formal SAT, equivalence/implication counterexamples, and CSP solver
- regex_lab: {"pattern": "^[a-z]+$", "text": "hello", "should_match": ["abc"], "should_not_match": ["123"]} -- deterministic regex tester, capture groups, substitutions, and ReDoS check
- code_graph: {"root": ".", "action": "summary|impact|cycles|calls", "target": ""} -- module dependency graph, circular imports, blast-radius impact, and Python call graph
- diff_semantic: {"path_a": "old.py", "path_b": "new.py", "text_a": "", "text_b": "", "kind": "auto|python|json|toml|ini"} -- AST-aware Python & structural JSON/TOML/INI diff
- reasoning_board: {"action": "init|add_fact|add_hypothesis|update_hypothesis|add_decision|audit|summary|clear", "board_id": "default", "text": "", "source": ""} -- structured cognitive scratchpad & contradiction audit
- symbol_verify: {"action": "verify_symbol|audit_code", "module": "json", "symbol": "loads", "kwargs": [], "code": "", "root": ""} -- verify Python stdlib/repo modules, attributes, kwargs, or audit code snippets for hallucinated APIs
- citation_audit: {"text": "", "root": ".", "claims": [], "sources": []} -- verify file:line citations, verbatim quotes, and source grounding
- fact_cross_check: {"text": "..."} -- catch prose arithmetic errors, table total mismatches, bad dates/weekdays, list counts, and self-contradictions
- consistency_check: {"action": "compare_candidates|audit_against_receipts", "candidates": [], "reply": "", "receipts": []} -- compare multiple answers for divergence or audit a reply against tool receipts
- plan_fallback: {"failed_tool": "run_code", "error": "...", "goal": "...", "tried_tools": []} -- classify a failure and plan ranked, policy-safe alternative tools/strategies
- runtime_resilience_check: {} -- inspect live model-call retry, context compaction, arg-repair, loop-breaker, and circuit-breaker telemetry
- prompt_shield: {"text": "...", "path": "", "redact_secrets": true} -- scan text or a guarded file for prompt injection, Trojan-Source Unicode, and secret leaks
- error_diagnose: {"error_text": "...", "root": ".", "max_frames": 8, "context_lines": 3} -- parse multi-language tracebacks/compiler errors and resolve repo source context
- code_audit: {"code": "", "path": "<file>", "language": "", "min_severity": "low"} -- static security & Python AST bug-footgun audit
- data_transform: {"action": "convert|profile|query|encode|decode|timestamp|cron", "data": "...", "path": "", "output_format": "json|csv|markdown|flat", "expression": ""} -- structured data conversion, stats profiling, encoding, timestamps, cron
- learn_tool: {"tool": "blender|ffmpeg|inkscape|<any tool>", "goal": "<task>", "subcommand": "", "research_web": false, "save_note": false} -- discover and figure out an unfamiliar/external CLI or package via live --version/--help, Python metadata, and optional docs
- data_inspect: {"path": "data/records.jsonl", "max_bytes": 256000}
- data_query: {"path": "data/records.jsonl", "sql": "", "projection_json": ["id", "/nested/name"], "filters_json": {"status": "active"}, "max_rows": 100, "max_columns": 50, "max_output_bytes": 256000, "max_scan_bytes": 4000000, "timeout": 5}
- data_convert: {"input_path": "data/records.jsonl", "output_path": "data/records.csv", "fields_json": ["id", "name"], "output_format": "csv", "apply": false, "max_input_bytes": 16000000, "max_output_bytes": 16000000, "max_rows": 10000, "max_columns": 100, "max_fields": 50, "max_field_bytes": 64000, "max_depth": 16, "preview_rows": 5, "timeout": 10}
- sqlite_mutate: {"path": "data/app.db", "sql": "UPDATE records SET status = ? WHERE id = ?", "parameters_json": ["done", 42], "mode": "preview|apply", "preview_token": "optional preview response token for drift-fenced apply", "max_rows": 1000, "timeout": 2, "max_db_bytes": 67108864}
- task_create: {"title": "...", "detail": "...", "priority": 2, "project": "...", "owner": "..."}
- task_list: {"status": ["pending", "blocked"], "project": "", "include_done": false, "limit": 50} -- also accepts legacy "pending|blocked"
- task_update: {"task_id": "...", "status": "in_progress|blocked|done", "note": "..."}
- task_show: {"task_id": "..."}
- task_delete: {"task_id": "..."}
- task_plan: {"title": "...", "steps": ["Step 1", "Step 2", {"title": "Step 3", "detail": "..."}], "project": "...", "sequential": true}
- task_progress: {"project": ""}
- task_ledger: {"goal_id": "...", "replan_count": 0, "last_replan_reason": ""}
- task_depend: {"task_id": "...", "depends_on": "...", "remove": false}
- checklist_create: {"title": "...", "items_json": ["Inspect", "Implement", "Validate", "Report"], "project": "..."}
- checklist_update: {"checklist_id": "...", "item": "1|id-prefix", "status": "in_progress|done|blocked", "note": "..."}
- checklist_show: {"checklist_id": "..."}
- command_registry_list: {"filter_text": "filesystem|dangerous|context"}
- activity_status: {}
- permission_policy: {"tool_name": "file_delete"}
- context_compaction_plan: {"session": "", "project": ""}
- memory_search: {"query": "...", "limit": 10}
- ground_artifact: {"artifact": "...", "checks_json": [{"type": "contains", "text": "..."}]}
- apply_learned: {"task": "...", "limit": 5}
- workflow_run: {"name": "...", "max_iterations": 1}
- diagnostics: {}
- context_health: {}
- learning_health_status: {}
- evaluation_history_status: {"model": "", "model_digest": "", "suite": "", "suite_version": "", "suite_digest": "", "tolerance": 0.0, "max_records": 10000}
- context_policy_status: {"context_size": "1m"}
- set_context_size: {"context_size": "256k"}
- memory_quality_report: {"sample_limit": 5}
- memory_quality_repair: {"apply": false}
- memory_privacy_review: {"sample_limit": 20}
- memory_privacy_repair: {"lesson_ids_json": ["lesson-id"], "apply": false}
- memory_embedding_backfill: {"limit": 25, "apply": false}
- memory_interaction_embedding_backfill: {"limit": 25, "apply": false}
- system_improvement_report: {}
- master_orchestrate: {"task": "...", "mode": "ask|inline|delegate|fleet", "agents": 24, "worker_cap": 24, "tier": "code", "project": "/path/to/repo"} -- worker_cap is a per-run override bounded by the operator ceiling
- master_status: {}
- master_capacity: {"requested_agents": 0, "worker_cap": 0}
- master_cancel: {"agent_id": "master-id|prefix|all"}
- master_retry: {"agent_id": "master-id|prefix", "tier": "code"}
- self_heal_check: {}
- self_heal_repair: {"apply": false}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- update_emotion_vectors: {"vectors_json": {"warmth": 0.5, "brevity": 0.2}, "mode": "merge|replace|clear|reset"}
- tune_emotion_vectors: {"feedback_text": "be warmer but more concise", "step": 0.1}
- learn_preference: {"text": "User prefers concise status updates.", "scope": "global"}
- preferences_status: {"include_disabled": false, "limit": 20}
- tool_manifest: {}
- report_progress: {"summary": "<milestone update>", "thinking": "", "phase": "progress"}
- post_to_board: {"text": "<update or @handle message>", "kind": "chat|status|handoff|praise|complaint|joke|result"}
- read_board: {"since_seq": 0, "limit": 20, "mentions_only": false}
- delegate_task: {"task": "<bounded subtask>", "name": "coder|scout|security|skeptic", "tier": "", "model": "", "project": "", "cwd": "", "max_steps": 8} -- depth 0 only; child runs independently and cannot delegate further
- spawn_agents: {"tasks": [{"task": "<subtask 1>", "name": "scout"}, {"task": "<subtask 2>", "name": "coder"}], "tier": "", "model": "", "project": "", "max_steps": 8} -- depth 0 only; parallel across Main and Node1
- ensemble_answer: {"prompt": "<question or design problem>", "tiers": "", "synth_tier": "", "num_predict": 700, "mode": "prose|code"} -- local-only multi-model synthesis; compounds answers from bound local models
- consult: {"prompt": "<question or decision to verify>", "tiers": ""} -- local-only multi-model second opinion with agree/disagree verdict
- offload: {"prompt": "...", "tier": "fast|code|general|reasoning|vision|cloud-code|cloud-general"}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""

REPOSITORY_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "tool_help",
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "directory_digest",
    "file_read_range", "context_pack",
    "repo_status", "repo_diff",
    "repo_log", "repo_show", "repo_blame",
    "project_detect", "data_inspect", "data_query", "archive_list", "artifact_risk_inspect",
    "verify_artifact",
    "text_search", "script_search", "program_search", "image_inspect",
    "document_extract", "media_inspect", "code_metrics", "schema_validate",
    "language_matrix", "math_eval", "logic_verify", "regex_lab", "code_graph",
    "diff_semantic", "symbol_verify", "citation_audit", "fact_cross_check",
    "consistency_check", "plan_fallback", "runtime_resilience_check", "prompt_shield",
    "error_diagnose", "code_audit", "data_transform", "command_registry_list",
    "activity_status", "permission_policy", "context_compaction_plan",
    "diagnostics", "context_health", "learning_health_status", "context_policy_status",
    "artifact_ground", "evaluation_history_status",
    "memory_quality_report", "memory_privacy_review", "system_improvement_report",
    "master_status", "master_capacity",
    "self_heal_check", "status", "system_profile_text", "environment_status",
    "toolchain_status", "hardware_profile",
    "tool_inventory", "output_digest",
    "emotion_vector_status", "preferences_status", "tool_manifest",
    "report_progress", "post_to_board", "read_board", "reasoning_board",
    "memory_search", "web_search", "web_fetch", "weather_lookup",
    "test_discover",
    "find_references", "diff_files",
})

REPOSITORY_READ_ONLY_FORBIDDEN_ARGS: frozenset[str] = frozenset({
    "token", "approval", "extra_roots",
})

REPOSITORY_AGENT_TOOL_HELP: str = """Available tools:
- tool_help: {"query": "search"}
The JSON values below are schema placeholders, not suggested filenames or
search terms. Replace every <...> value with an exact task-relevant path,
symbol, filename, or glob. For a code/symbol audit, start with text_search for
an exact symbol named by the task; do not default to Python or server.py.
- file_policy: {}
- workspace_inventory: {"path": ".", "max_entries": 20000, "timeout_seconds": 10, "top_n": 15}
- workspace_compare: {"left": "<first task-relevant file or directory>", "right": "<second task-relevant file or directory>", "max_entries": 2000, "max_file_bytes": 64000000, "max_total_bytes": 256000000, "max_details": 1000, "max_output_bytes": 256000, "timeout": 5}
- repo_status: {"root": ".", "timeout": 10, "max_output": 128000}
- repo_diff: {"root": ".", "staged": false, "path": "", "context": 3, "timeout": 10, "max_output": 128000}
- project_detect: {"path": ".", "max_depth": 8, "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_results": 500}
- dependency_inventory: {"path": ".", "max_depth": 5, "max_files": 100, "max_total_bytes": 2000000, "max_results": 2000}
- directory_tree: {"path": ".", "depth": 2, "max_entries": 200}
- file_find: {"query": "<task-relevant filename or glob>", "root": ".", "max_results": 50}
- repository_symbol_index: {"path": ".", "glob": "<task-relevant source glob>", "language": "auto|python|javascript|typescript|c|cpp|csharp|rust|go", "max_files": 200, "max_total_bytes": 2000000, "max_file_bytes": 256000, "max_symbols": 2000}
- file_read: {"path": "<task-relevant relative path>", "max_bytes": 256000}
- file_digest: {"path": "<task-relevant relative file>", "max_bytes": 32000000}
- directory_digest: {"path": ".", "max_depth": 12, "max_files": 2000, "max_total_bytes": 32000000, "max_file_bytes": 32000000, "max_results": 2500}
- file_read_range: {"path": "<task-relevant relative path>", "start_line": 1, "end_line": 200}
- context_pack: {"paths_json": ["<task-relevant relative path>", "<another task-relevant relative path>"], "max_files": 12, "max_total_bytes": 256000, "max_bytes_per_file": 64000}
- repo_log: {"path": ".", "revision": "HEAD", "file_path": "<optional contained relative path>", "count": 20, "timeout": 5, "max_bytes": 256000}
- repo_show: {"path": ".", "revision": "HEAD", "file_path": "<required contained relative file>", "timeout": 5, "max_bytes": 256000}
- repo_blame: {"path": ".", "file_path": "<required contained relative file>", "revision": "HEAD", "start_line": 1, "end_line": 100, "timeout": 5, "max_bytes": 256000}
- archive_list: {"path": "<task-relevant ZIP or TAR>", "max_entries": 2000, "max_total_bytes": 256000000, "max_ratio": 100, "max_results": 2500}
- artifact_risk_inspect: {"path": "<task-relevant document, executable, script, or binary>", "max_scan_bytes": 16777216, "max_seconds": 5}
- verify_artifact: {"path": "<task-relevant downloaded installer, archive, or image>", "expect_type": "pe|msi|zip|iso|elf", "expect_publisher": "<exact complete certificate organization O= value>", "sha256": "<64-hex digest>"}
- log_inspect: {"path": "<task-relevant log file>", "tail_lines": 0, "context_lines": 2, "max_file_bytes": 64000000, "max_scan_bytes": 4000000, "max_lines": 10000, "max_line_bytes": 4096, "max_results": 100, "max_output_bytes": 256000, "timeout": 5}
- text_search: {"query": "<exact task symbol or anchor>", "root": ".", "glob": "<task-relevant glob>", "max_results": 100}
- script_search: {"query": "<task-relevant script name>", "root": ".", "max_results": 100}
- program_search: {"query": "<required program name>", "max_results": 50}
- environment_status: {"refresh": false}
- toolchain_status: {"name": "cargo|git|cmake|...", "refresh": false} -- fixed local version probe; no command, executable path, or arguments
- tool_inventory: {"category": "compiler|build_system|test_runner|...", "name": "", "refresh": false} -- categorized installed-tool inventory; paths redacted
- output_digest: {"path": "<task-relevant log file>", "job_id": "", "tail_lines": 20} -- exactly one of path or job_id; bounded failure summary
- hardware_profile: {"workload": "general|chat|coding|agentic|research", "refresh": false}
- image_inspect: {"path": "<task-relevant image path>"}
- document_extract: {"path": "<task-relevant DOCX/XLSX/PPTX/HTML/SVG/SRT/VTT path>", "max_chars": 32000, "max_items": 200}
- media_inspect: {"path": "<task-relevant WAV/MIDI/AVI/GLB/glTF/OBJ/STL path>"}
- code_metrics: {"path": ".", "max_files": 2000, "top_n": 10}
- schema_validate: {"path": "<task-relevant JSON/JSONL/TOML path>", "schema_path": "<schema JSON path>", "schema_json": ""}
- language_matrix: {"language": ""}
- math_eval: {"expression": "<expression or equation>", "action": "eval|solve|matrix|convert|date|derivative|integrate", "exact": false}
- logic_verify: {"formula": "<propositional formula>", "action": "satisfiable|truth_table|equivalent|implies|cnf|csp", "formula_b": ""}
- regex_lab: {"pattern": "<regex>", "text": "<sample text>", "should_match": [], "should_not_match": []}
- code_graph: {"root": ".", "action": "summary|impact|cycles|calls", "target": "<file, module, or symbol>"}
- diff_semantic: {"path_a": "<first file>", "path_b": "<second file>", "text_a": "", "text_b": "", "kind": "auto"}
- reasoning_board: {"action": "init|add_fact|add_hypothesis|update_hypothesis|add_decision|audit|summary|clear", "board_id": "default", "text": ""}
- symbol_verify: {"action": "verify_symbol|audit_code", "module": "<module>", "symbol": "<attr>", "code": "", "root": ""}
- citation_audit: {"text": "<draft with file:line citations>", "root": ".", "claims": [], "sources": []}
- fact_cross_check: {"text": "<draft prose>"}
- consistency_check: {"action": "compare_candidates|audit_against_receipts", "candidates": [], "reply": "", "receipts": []}
- plan_fallback: {"failed_tool": "<tool>", "error": "<error>", "goal": "<task>", "tried_tools": []}
- runtime_resilience_check: {}
- prompt_shield: {"text": "<untrusted text>", "path": "", "redact_secrets": true}
- error_diagnose: {"error_text": "<traceback or compiler error>", "root": ".", "max_frames": 8, "context_lines": 3}
- code_audit: {"code": "", "path": "<task-relevant source file>", "language": "", "min_severity": "low"}
- data_transform: {"action": "convert|profile|query|encode|decode|timestamp|cron", "data": "", "path": "", "output_format": "json", "expression": ""}
- data_inspect: {"path": "<task-relevant data file>", "max_bytes": 256000}
- data_query: {"path": "<task-relevant SQLite, JSON, JSONL, CSV, or TSV file>", "sql": "<SQLite SELECT/CTE only, otherwise empty>", "projection_json": ["<field or JSON pointer>"], "filters_json": {"<field or JSON pointer>": "<exact value>"}, "max_rows": 100, "max_columns": 50, "max_output_bytes": 256000, "max_scan_bytes": 4000000, "timeout": 5}
- memory_search: {"query": "...", "limit": 10}
- web_search: {"query": "...", "limit": 5}
- web_fetch: {"url": "https://...", "max_chars": 8000}
- weather_lookup: {"location": "Chicago, IL|60601", "forecast_days": 3, "units": "auto|metric|imperial"}
- test_discover: {"root": ".", "framework": "auto"}
- find_references: {"root": ".", "symbol": "<exact symbol named by the task>", "glob": "<task-relevant source glob>"}
- diff_files: {"root": ".", "left": "<first task-relevant file>", "right": "<second task-relevant file>", "context": 3}
- command_registry_list: {"filter_text": "filesystem|context|status"}
- activity_status: {}
- permission_policy: {"tool_name": "file_read"}
- context_compaction_plan: {"session": "", "project": ""}
- diagnostics: {}
- context_health: {"session": "", "project": ""}
- learning_health_status: {}
- evaluation_history_status: {"model": "", "model_digest": "", "suite": "", "suite_version": "", "suite_digest": "", "tolerance": 0.0, "max_records": 10000}
- artifact_ground: {"path": "artifacts/generated/report", "recipe": "auto", "requirements_json": {}}
- context_policy_status: {"context_size": "32k"}
- memory_quality_report: {"sample_limit": 5}
- memory_privacy_review: {"sample_limit": 20}
- system_improvement_report: {"session": "", "project": ""}
- master_status: {}
- master_capacity: {"requested_agents": 0, "worker_cap": 0}
- self_heal_check: {}
- status: {}
- system_profile_text: {}
- emotion_vector_status: {}
- preferences_status: {"include_disabled": false, "limit": 20}
- tool_manifest: {}
- report_progress: {"summary": "<milestone update>", "thinking": "", "phase": "progress"}
- post_to_board: {"text": "<update or @handle message>", "kind": "chat|status|handoff|praise|complaint|joke|result"}
- read_board: {"since_seq": 0, "limit": 20, "mentions_only": false}

Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}, "reason": "short reason"}
or
{"final": "your final answer"}
"""

AGENT_SYSTEM_OPERATOR_TOOLS: frozenset[str] = frozenset({
    "record_outcome",
    "admin_login", "admin_register", "admin_set_account", "admin_accounts",
    "elevate", "permission_mode", "permission_rule_set", "permission_approve",
    "runtime_policy_update", "update_system_profile",
    "runtime_source_update", "runtime_source_stash",
    "autopilot_start", "autopilot_resume", "autopilot_pause", "autopilot_cancel", "mission_start",
    "self_heal_repair", "memory_export", "memory_privacy_repair",
    "memory_quality_repair", "workflow_list", "workflow_save", "workflow_delete", "workflow_run",
    "set_context_size", "unload", "update_emotion_vectors",
    "tune_emotion_vectors", "learn_preference",
    "ollama_pool_admin_status", "cloud_opt_in",
})

PROJECT_SCOPED_PATH_TOOLS: frozenset[str] = frozenset({
    "file_check",
    "file_read", "file_digest", "directory_digest", "file_read_range", "context_pack", "workspace_compare",
    "repo_log", "repo_show", "repo_blame",
    "data_inspect", "data_query", "data_convert", "sqlite_mutate", "image_inspect",
    "document_extract", "media_inspect", "code_metrics", "schema_validate", "code_graph",
    "diff_semantic", "symbol_verify", "citation_audit", "prompt_shield", "error_diagnose",
    "code_audit", "data_transform", "log_inspect", "file_write", "file_batch_write",
    "json_patch", "file_edit", "text_patch",
    "archive_list", "archive_extract",
    "file_delete", "directory_create", "workspace_inventory", "dependency_inventory", "directory_tree",
    "file_find", "repository_symbol_index", "text_search", "script_search", "artifact_verify",
    "fetch_artifact", "verify_artifact",
    "artifact_ground", "artifact_risk_inspect", "scaffold_project", "archive_create",
    "repo_status", "repo_diff", "project_detect", "file_copy", "file_move",
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag", "git_merge", "git_cherry_pick",
    "build_run", "build_clean",
    "ensemble_codegen_build_loop",
    "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
    "output_digest",
})

PROJECT_SCOPED_EXECUTION_TOOLS: frozenset[str] = frozenset({"workspace_run", "script_run"})

DEVELOPER_WORKFLOW_TOOLS: frozenset[str] = frozenset({
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
    "git_merge", "git_cherry_pick",
    "build_run", "build_clean",
    "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
})

PROJECT_SCOPED_ROOT_AND_PATH_TOOLS: frozenset[str] = frozenset({
    "test_run", "lint_run", "format_code", "typecheck_run",
})

PROJECT_SCOPED_COMMAND_TOOLS: frozenset[str] = frozenset({"build_run"})

PROJECT_BOUND_AGENT_TOOLS: frozenset[str] = (
    PROJECT_SCOPED_PATH_TOOLS
    | PROJECT_SCOPED_EXECUTION_TOOLS
    | frozenset({
        "agent_lane", "ground_artifact", "program_search", "tool_help",
        "web_search", "web_fetch",
        "weather_lookup", "approximate_location_lookup", "memory_search",
        "file_policy", "task_create", "task_list", "task_update", "task_show",
        "task_delete", "task_plan", "task_progress", "task_ledger", "task_depend",
        "checklist_create", "checklist_update", "checklist_show",
        "command_registry_list", "tool_manifest", "activity_status",
        "permission_policy", "context_compaction_plan", "diagnostics",
        "context_health", "learning_health_status", "memory_quality_report",
        "memory_privacy_review", "evaluation_history_status",
        "system_improvement_report", "master_status",
        "master_capacity", "self_heal_check", "status", "system_profile_text",
        "emotion_vector_status", "preferences_status", "context_policy_status",
        "delegate_task", "spawn_agents", "report_progress", "post_to_board", "read_board",
        "ensemble_answer", "consult",
        "environment_status", "toolchain_status", "hardware_profile",
        "tool_inventory", "output_digest", "language_matrix",
        "math_eval", "logic_verify", "regex_lab", "reasoning_board",
        "fact_cross_check", "consistency_check", "plan_fallback", "runtime_resilience_check",
        "learn_tool",
        "process_list", "process_memory_risk_inspect",
    })
)

CLOUD_AGENT_NESTED_MODEL_TOOLS: frozenset[str] = frozenset({
    "offload", "master_orchestrate", "master_retry", "workflow_run",
    "delegate_task", "spawn_agents",
    "ensemble_answer", "consult",
    "game_reference_suite", "game_generate_and_test", "game_generation_campaign",
    "ensemble_codegen_build_loop",
})

CLOUD_AGENT_LOCAL_ONLY_TOOLS: frozenset[str] = frozenset({
    "file_check",
    "agent_lane",
    "environment_status", "toolchain_status", "hardware_profile", "file_policy",
    "tool_inventory", "output_digest",
    "workspace_inventory", "directory_tree", "file_find", "file_read",
    "file_read_range", "file_digest", "text_search", "repo_status",
    "repo_diff", "artifact_risk_inspect", "process_list",
    "process_memory_risk_inspect", "image_inspect", "document_extract", "media_inspect",
    "code_metrics", "schema_validate", "language_matrix", "math_eval", "logic_verify",
    "regex_lab", "code_graph", "diff_semantic", "symbol_verify", "citation_audit",
    "fact_cross_check", "consistency_check", "plan_fallback", "runtime_resilience_check",
    "prompt_shield", "error_diagnose", "code_audit", "data_transform", "learn_tool",
})

AGENT_WEB_GATED_TOOLS: frozenset[str] = frozenset({
    "web_search", "web_fetch", "weather_lookup", "approximate_location_lookup",
})

AGENT_LOCATION_GATED_TOOLS: frozenset[str] = frozenset({"approximate_location_lookup"})

# Tools that read or mutate global operator memory, saved workflows, unscoped
# master/lane orchestration, or process-global profile/context state.  They are
# withheld from advertisement and refused at dispatch whenever an autonomous
# agent run is executing inside a hosted account boundary.
ACCOUNT_SCOPED_AGENT_BLOCKED_TOOLS: frozenset[str] = frozenset({
    # Global memory / learning / playbook / session store
    "apply_learned",
    "calibration_status",
    "evaluation_history_status",
    "extract_grounded",
    "learn_from_example",
    "learn_tiers",
    "learn_tool",
    "learning_health_status",
    "memory_embedding_backfill",
    "memory_export",
    "memory_interaction_embedding_backfill",
    "memory_privacy_repair",
    "memory_privacy_review",
    "memory_quality_repair",
    "memory_quality_report",
    "memory_search",
    "playbook_note",
    "playbook_read",
    "recall",
    "record_outcome",
    "session_export",
    "sonder_authoritative_indexes",
    "sonder_forget_fact",
    "sonder_remember_fact",
    "sonder_sessions",
    "sonder_stats",
    "turn_inspect",
    # Operator saved workflow library & unscoped game generators
    "game_campaign",
    "game_generate",
    "game_generate_and_test",
    "game_generation_campaign",
    "workflow_delete",
    "workflow_list",
    "workflow_run",
    "workflow_save",
    # Unscoped master / retry / self-heal / standalone local-owner lanes / cross-tenant ledgers
    "agent_cancel",
    "agent_capacity",
    "agent_lane",
    "agent_retry",
    "agent_status",
    "autopilot_status",
    "batch_job_recent",
    "batch_job_results",
    "batch_job_status",
    "composition_bindings",
    "computer_use_status",
    "loop",
    "master",
    "master_cancel",
    "master_capacity",
    "master_orchestrate",
    "master_retry",
    "master_status",
    "mission_start",
    "mission_status",
    "model_fanout_recent",
    "model_fanout_status",
    "openrouter_account",
    "reasoning_show",
    "runtime_source_stash_status",
    "runtime_source_update_status",
    "self_heal_check",
    "self_heal_repair",
    # Operator-global profile / emotion / preferences / process context
    "context_compaction_plan",
    "context_health",
    "diagnostics",
    "emotion_vector_status",
    "improvement_report",
    "learn_preference",
    "preferences_status",
    "set_context_size",
    "system_improvement_report",
    "system_profile_text",
    "tune_emotion_vectors",
    "update_emotion_vectors",
})

RENDERED_VERDICT_TOOLS: frozenset[str] = frozenset({
    "apply_patch", "build_clean", "build_run", "dependency_add",
    "dependency_audit", "dependency_remove", "dependency_update", "format_code",
    "git_branch", "git_checkout", "git_cherry_pick", "git_commit", "git_merge",
    "git_stash", "git_tag", "lint_run", "script_run", "test_run",
    "typecheck_run", "workspace_run",
    "run_code", "run_project",
    "ground_artifact", "artifact_ground", "artifact_verify",
})

SPECULATABLE_ARGFREE_TOOLS: frozenset[str] = frozenset({
    "workspace_inventory", "dependency_inventory", "directory_tree", "status", "activity_status",
    "context_health", "command_registry_list",
})

WORK_VALIDATION_TOOLS: frozenset[str] = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "ground_artifact", "artifact_ground",
    "artifact_verify", "game_reference_suite", "game_generate_and_test",
    "game_generation_campaign", "self_heal_check", "workspace_inventory", "directory_tree", "file_find",
    "repository_symbol_index", "file_read", "file_read_range", "archive_extract", "archive_create",
    "text_search", "image_inspect", "document_extract", "media_inspect", "code_metrics",
    "schema_validate", "code_graph", "diff_semantic", "symbol_verify", "citation_audit",
    "prompt_shield", "error_diagnose", "code_audit", "data_transform",
    "memory_quality_report", "memory_privacy_review", "learning_health_status",
})

WORK_INSPECTION_TOOLS: frozenset[str] = frozenset({
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "directory_digest", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "file_read_range", "context_pack",
    "text_search", "script_search", "program_search", "image_inspect", "document_extract",
    "media_inspect", "code_metrics", "schema_validate", "language_matrix", "math_eval",
    "logic_verify", "regex_lab", "code_graph", "diff_semantic", "symbol_verify",
    "citation_audit", "fact_cross_check", "consistency_check", "plan_fallback",
    "runtime_resilience_check", "prompt_shield", "error_diagnose", "code_audit",
    "data_transform", "learn_tool", "repo_status", "repo_diff",
    "repo_log", "repo_show", "repo_blame",
    "data_inspect", "data_query", "project_detect", "archive_list", "artifact_risk_inspect",
    "verify_artifact",
    "memory_search", "learning_health_status", "evaluation_history_status",
    "memory_quality_report", "memory_privacy_review", "artifact_ground",
    "web_search", "web_fetch", "weather_lookup", "approximate_location_lookup",
    "status", "diagnostics", "toolchain_status", "process_list", "process_memory_risk_inspect",
    "tool_inventory", "output_digest",
    "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
    "dependency_audit", "find_references", "diff_files", "secret_scan",
    "build_run",
    "task_progress", "task_ledger",
})

RUNTIME_OBSERVATION_TOOLS: frozenset[str] = frozenset({
    "task_list", "task_show", "checklist_show",
    "admin_status", "admin_whoami",
    "autopilot_status", "calibration_status",
    "learn_tiers", "live_reload_status", "mcp_runtime_status",
    "reasoning_show", "sonder_sessions", "sonder_stats",
    "turn_inspect", "workflow_list", "memory_export",
    "runtime_policy_status",
})

UNCONFINED_ROOT_TOOLS: frozenset[str] = frozenset({
    "diff_files", "find_references", "secret_scan",
})

AGENT_FILE_EVIDENCE_TOOLS: frozenset[str] = frozenset({
    "workspace_inventory", "workspace_compare", "directory_tree", "file_read", "file_read_range",
    "file_digest", "directory_digest", "file_find", "text_search",
    "script_search", "image_inspect", "document_extract", "media_inspect", "code_metrics",
    "schema_validate", "code_graph", "diff_semantic", "symbol_verify", "citation_audit",
    "prompt_shield", "error_diagnose", "code_audit", "data_transform", "log_inspect",
    "data_inspect", "data_query", "project_detect",
    "context_pack", "repo_log", "repo_show", "repo_blame", "archive_list",
    "dependency_inventory", "artifact_risk_inspect",
})

AGENT_DEDUPLICATED_INSPECTION_TOOLS: frozenset[str] = frozenset({
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree", "directory_digest", "file_find",
    "dependency_inventory",
    "repository_symbol_index", "log_inspect", "file_read", "file_digest", "file_read_range", "context_pack",
    "data_inspect", "data_query", "text_search", "script_search",
    "program_search", "image_inspect", "document_extract", "media_inspect", "code_metrics",
    "schema_validate", "language_matrix", "math_eval", "logic_verify", "regex_lab",
    "code_graph", "diff_semantic", "symbol_verify", "citation_audit", "fact_cross_check",
    "consistency_check", "plan_fallback", "runtime_resilience_check", "prompt_shield",
    "error_diagnose", "code_audit", "data_transform", "learn_tool", "environment_status",
    "toolchain_status", "hardware_profile", "repo_status", "repo_diff", "project_detect",
    "tool_inventory", "output_digest",
    "repo_log", "repo_show", "repo_blame", "archive_list", "artifact_risk_inspect",
    "process_list", "process_memory_risk_inspect",
})

AGENT_EXECUTION_STATE_INVALIDATION_TOOLS: frozenset[str] = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "workflow_run",
})


def build_decision_repair_prompt(
    step_prompt: str,
    *,
    attempt: int,
    repair_limit: int,
    require_final: bool,
    error: object,
    raw: str,
    length_limited: bool,
    write_chunk_hint: int,
) -> str:
    """Build the bounded format-repair prompt for an invalid agent decision."""
    valid_shape = (
        '{"final":"answer"}'
        if require_final
        else '{"tool":"name","args":{},"reason":"brief"} or {"final":"answer"}'
    )
    recovery = ""
    if length_limited:
        recovery = (
            "\nHOST LENGTH RECOVERY: the previous decision hit its output "
            "limit. Do not repeat the oversized response. For a large "
            "file_write, emit one valid chunk of at most %d characters now "
            "and use append on a later tool turn; otherwise simplify the "
            "decision."
            % write_chunk_hint
        )
    return (
        step_prompt
        + "\n\nHOST FORMAT REPAIR %d/%d: Your previous response was invalid. "
        "Return exactly one JSON object and no prose or Markdown. Use %s.\n"
        "Parser error: %s%s\nPrevious response excerpt:\n%s"
        % (
            attempt + 1,
            repair_limit,
            valid_shape,
            error,
            recovery,
            str(raw or "")[:1000],
        )
    )


def build_claim_review_prompt(
    task: str,
    final: str,
    observation_prompt: str,
    vocabulary: Sequence[str],
) -> str:
    """Build the negative-claim reviewer prompt bounded to the run's vocabulary."""
    return (
        "Task:\n%s\n\nProposed final:\n%s\n\n%s\n\n"
        "JSON schema: {\"decision\":\"accept|continue\",\"reason\":\"brief\","
        "\"tool\":\"%s or empty\","
        "\"args\":{}}"
        % (
            str(task or "")[:8000],
            str(final or "")[:4000],
            observation_prompt,
            "|".join(vocabulary),
        )
    )


def build_turn_transcript(
    *,
    prompt: str,
    tool_help: str,
    unsafe: bool,
    unsafe_warning: str,
    project_scope: str,
    standing_notice: str,
    delegation_preamble: str,
) -> str:
    """Compose the static prefix transcript for one agent turn."""
    transcript = "Task:\n%s\n\n%s" % (prompt, tool_help)
    if unsafe:
        transcript = "%s\n\n%s" % (unsafe_warning, transcript)
    if project_scope:
        transcript += (
            "\n\nPROJECT ROOT: %s\nYour file/inspection tools are rooted at this "
            "directory: a relative path or '.' inspects the PROJECT, not Sonder's "
            "own workspace. Use paths relative to the project root." % project_scope
        )
    transcript += "\n\n" + standing_notice
    if delegation_preamble:
        transcript += "\n\n" + delegation_preamble
    return transcript


def build_finalization_prompt(transcript: str, observation_prompt: str) -> str:
    """Build the budget-exhausted finalization prompt."""
    prompt = transcript
    if observation_prompt:
        prompt += "\n\n" + observation_prompt
    prompt += (
        "\n\nHOST FINALIZATION ONLY: the tool-step budget is exhausted. Do not call "
        "another tool. Synthesize a concise grounded result from the observations, "
        "disclose unresolved errors or checks, and return exactly "
        '{"final":"answer"}.'
    )
    return prompt


def build_loop_guard_finalize_prompt(transcript: str, observation_prompt: str) -> str:
    """Build the forced finalization prompt after repeated cached inspections."""
    prompt = transcript
    if observation_prompt:
        prompt += "\n\n" + observation_prompt
    prompt += (
        "\n\nHOST LOOP GUARD: the same successful inspection was "
        "requested repeatedly. Do not call another tool. Return a "
        "final answer now using only the host observations above; "
        "do not claim any unobserved mutation or validation."
    )
    return prompt


def attach_tool_evidence(
    text: object,
    observations: Sequence[str],
    *,
    include_evidence: bool,
) -> str:
    """Append exactly one host-owned evidence section after neutralizing model markers."""
    rendered = str(text or "")
    marker = "=== TOOL EVIDENCE ==="
    if include_evidence and observations:
        rendered = rendered.replace(marker, "=== UNTRUSTED TOOL EVIDENCE MARKER ===")
        rendered += "\n\n%s\n%s" % (marker, "\n\n".join(observations))
    return rendered


def build_failure_recovery_hint(
    tool_name: str,
    observation_text: str,
    *,
    blocks_completion: bool,
) -> str:
    """Build the deterministic host recovery instruction appended to failed observations."""
    if blocks_completion:
        recovery = (
            "HOST RECOVERY: this required host observation blocks "
            "completion until you retry this exact call and it succeeds."
        )
    else:
        recovery = (
            "HOST RECOVERY: do not repeat this exact failed call unchanged. "
            "Inspect the error and change the target, arguments, or tool."
        )
    lowered = observation_text.lower()
    if tool_name == "script_run":
        recovery += (
            " Use script_search/file_find to locate a real script, or use "
            "workspace_run with an approved interpreter and explicit argv."
        )
    elif tool_name == "file_write" and "file exists" in lowered:
        recovery += (
            " To replace the existing file, repeat the call with "
            "mode=overwrite."
        )
    elif tool_name == "file_edit" and "old text must not be empty" in lowered:
        recovery += (
            " file_edit replaces a non-empty old text; read the file "
            "first, or use file_write with mode=overwrite to rewrite it."
        )
    return recovery
