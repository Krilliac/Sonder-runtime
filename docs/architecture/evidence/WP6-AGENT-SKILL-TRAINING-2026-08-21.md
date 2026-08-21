# WP6 Agent, Skill, and Training Boundaries

Date: 2026-08-21

## Scope

This checkpoint composes the existing agent delegation, skill discovery, procedural publication, and attended-training boundaries into the migration ledger. It preserves provider and policy decisions at their ports and records tested behavior as `implemented_unverified`; external provider execution and production qualification remain outside this checkpoint.

## Evidence

- Agent presets, lineage, workspace isolation, budgets, continuations, and structured delegation are implemented in `sonder_runtime/application/agents/` and `sonder_runtime/application/subagents/`.
- Skill registry, progressive discovery, refresh, trust/policy handling, and procedural publication are implemented in `sonder_runtime/application/skills/`, `sonder_runtime/application/skill_discovery/`, and `sonder_runtime/application/skill_refresh.py`.
- Reproducible training metadata, qualification, dataset provenance, attended execution, adapter catalog, and cheap-learning routing are implemented in `sonder_runtime/application/training/` and `sonder_runtime/domain/training/`.

## Verification

Focused command: `python -m pytest tests/test_agent001_unified_composition.py tests/test_remaining_agent_004_008_009.py tests/test_remaining_agent_010.py tests/test_remaining_agent_005_job_integration.py tests/test_remaining_durable_subagents.py tests/test_skill_registry.py tests/test_skill_refresh_plugin_manifest.py tests/test_wp4_skill_registry.py tests/test_remaining_training_002_003_004_008.py tests/test_adaptive_training_boundary.py tests/test_attended_training_execution.py tests/test_wp7_training_catalog.py tests/test_training_data.py tests/test_training_tasks.py tests/test_train006_route_activation.py --basetemp C:\Users\Nathan\Documents\Codex\pytest-wp6-agents-skills-training -q`

Result: 69 passed, 1 known pytest cache-permission warning.

## Limitations

No external model provider, durable production fleet, or hardware qualification run is claimed here. Those remain integration or operational obligations.
