# WP5 AGENT-003 — Roles, presets, and budgets

This slice adds provider-neutral `AgentRole`, immutable role budgets, and a
small deterministic built-in preset catalog. Resolution normalizes names and
rejects any preset whose role budget widens a supplied parent ceiling.

Evidence: `tests/test_wp5_roles_presets_budgets.py` covers deterministic role
budgets, case-insensitive lookup, and parent-budget rejection. This is an
implementation foundation; formal checklist credit remains pending the full
WP5 integration and requirement audit.
