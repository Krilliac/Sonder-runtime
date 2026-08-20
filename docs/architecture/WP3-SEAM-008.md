# WP3-SEAM-008 — SkillRegistry contract

## Boundary

`sonder_runtime.domain.skills.registry` defines the pure skill record,
progressive discovery levels (`index`, `metadata`, `content`), source
provenance, validation results, compatibility, and trust/policy fields.
`sonder_runtime.application.skills.registry.SkillRegistry` owns a deterministic,
in-memory index and publishes only validated records.

Discovery is deliberately progressive: index results identify a skill without
loading its instructions; metadata adds descriptive and provenance fields;
content is returned only when explicitly requested. A missing content
materialization is not treated as a failed skill.

Source metadata records kind, locator, provider, version, revision, digest, and
signature. Validation is represented separately from trust: a structurally
valid skill is still untrusted and policy-denied unless an owning policy
surface explicitly supplies those facts. The registry does not execute skills,
load files, verify signatures, or modify existing skill loaders.

## Scope

This slice changes only the new application/domain registry modules, focused
tests, and this evidence document. Adapters and existing loaders remain out of
scope.

## Evidence

- `tests/test_skill_registry.py` covers progressive materialization, metadata,
  validation/trust separation, rejection, sorting, filtering, and policy
  exclusion.
- Focused test command: `python -m pytest tests/test_skill_registry.py`.
- No specification checkbox or evidence-status file is modified.
