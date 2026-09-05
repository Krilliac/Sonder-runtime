# Shared control-path source manifest

This manifest records the bounded shared-resolver inventory at this source
revision. The test `test_database_manifest_matches_active_shared_literal_resolvers`
checks database calls in the listed active composition/provider sources plus the
migration store registry against `STATE_DATABASES`. It does not claim to discover
arbitrary constructor-injected storage at runtime; those actual paths are required
through the trusted supplemental snapshot before host admission.

| Shared source | Inventory |
|---|---|
| bootstrap/app.py | sessions, jobs, execution-spill (same SONDER_JOBS_DB override), child-sessions, extensions; tool audit; lane-test catalog; STATE_HOME task memory fallback |
| platform/paths.py | memory SONDER_DB (including canonical home default); state_path configured-home precedence; no migration or mkdir invoked by inventory |
| adapters/security/approval_ledger.py | approvals.db / SONDER_APPROVALS_DB |
| adapters/persistence/migrations.py `_STORE_FILENAMES` | autopilot, fleet, operations, queued_actions, updates, jobs and their declared environment names |
| adapters/persistence/queued_actions.py | queued_actions.db / SONDER_QUEUED_ACTION_DB |
| adapters/persistence/served_action_receipts.py | served_action_receipts.db / SONDER_SERVED_ACTION_RECEIPTS_DB |
| adapters/persistence/composition_store.py | composition.db / SONDER_COMPOSITION_DB |
| adapters/persistence/fanout_store.py | fanout.db / SONDER_FANOUT_DB |
| adapters/persistence/fleet_store.py and lane_owner.py | fleet-principal.json / SONDER_FLEET_PRINCIPAL_FILE; lane-owner-<32hex>.lock beside fleet database |
| adapters/embedding_cache.py | embed-cache.db / SONDER_EMBED_CACHE_DB |
| adapters/runtime_policy.py + filesystem/atomic_json.py | SONDER_RUNTIME_POLICY or state runtime_policy.json; .lock, .tmp-*, .transition.json and its lock/temp family |
| adapters/secrets.py | SONDER_ROTATION_STATE or home/secrets/rotation.json; atomic .tmp-PID family; home/secrets directory |
| adapters/persistence/migrations.py | home/locks owned migration-lock directory |
| platform/speculation.py | state branch_predictor.json |
| adapters/accelerators/npu/service.py and manifest.py | npu-shadow-ledger.json / SONDER_NPU_SHADOW_LEDGER; npu-manifests / SONDER_NPU_MANIFEST_DIR owned directory |
| __main__.py `_configured_path` | conventional home sonder.toml/sonder.env or SONDER_CONFIG/SONDER_SECRETS; explicit CLI paths must be supplied separately |
| adapters/filesystem/workflow_store.py | home/workflows.json; existing file_ops retains workspace-configured workflow/roots/permissions protections |
| adapters/filesystem/file_ops.py | relocated SONDER_FILE_ROOTS_FILE plus home/workspace roots files and permissions; workspace-relative SONDER_WORKFLOWS, SONDER_EMOTION_VECTORS and SONDER_SYSTEM_PROFILE paths are also in admission inventory |
| adapters/security/unsafe_lab.py `_audit_path` | actual pure unsafe-lab audit resolver, including its distinct OS fallback |

Every database gets WAL, SHM and rollback-journal siblings. Atomic files get their
lock and bounded-name temp family, and audits get their actual rotation filename
family. These are filename families, not blanket ordinary-sibling protection.

Constructor-only state includes terminal output, explicitly supplied configuration
or secrets files, private model/artifact/configuration providers, and any alternate
policy or observation store supplied by host composition. Use `databases`,
`files`, `atomic_files`, `owned_directories`, `owner_lock_directories`, and
`audit_files` from the exact composed adapters. Current observation ledger bytes
are inline in fleet projections, so no separate observation DB is invented.
Artifact receiver root is available from its existing typed-config resolver.
The default inventory cannot certify completeness for unknown plugin/provider
constructors; composition must supply those paths or keep admission unavailable.
