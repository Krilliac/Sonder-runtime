# Master-spec evidence tracking design

**Status:** Adopted locally; CI freshness integration pending
**Applies to:** `SONDER-MASTER-IMPLEMENTATION-SPEC.md`

## Objective

Make every checked requirement independently auditable while keeping the master spec
readable. The Markdown checkbox is the human index; a separate append-only JSONL ledger
is the machine-readable evidence authority.

## Requirement identity

The master spec currently defines 204 unique IDs in the form `FAMILY-NNN`. IDs are
permanent once merged:

- Never renumber an existing ID to improve presentation.
- A materially changed requirement receives a new ID and supersedes the old one.
- Removed requirements remain in the ledger with `superseded` or `rejected` status.
- Work-package and final acceptance checkboxes without IDs are derived gates; they become
  checked only when their referenced requirements and explicit acceptance evidence pass.

## Proposed files

```text
docs/architecture/
  SONDER-MASTER-IMPLEMENTATION-SPEC.md
  evidence/
    requirements.jsonl
    artifacts/
      <requirement-id>/README.md
  generated/
    requirement-status.md
    requirement-status.json
```

`requirements.jsonl` is append-only by logical record version: update a requirement by
appending a higher `revision`, never by silently deleting its prior evidence. Generated
status files select the highest valid revision for each ID.

## Evidence record schema

```json
{
  "schema": "sonder-requirement-evidence-v1",
  "requirement_id": "ARCH-001",
  "revision": 1,
  "status": "verified",
  "claim": "Every core capability has one production implementation path.",
  "baseline_sha": "<40-hex>",
  "verified_sha": "<40-hex>",
  "pr": "https://github.com/Krilliac/Sonder-runtime/pull/000",
  "evidence": [
    {
      "kind": "test",
      "locator": "tests/test_architecture.py::test_one_authoritative_path",
      "result": "passed"
    },
    {
      "kind": "command",
      "locator": "python scripts/check_architecture.py",
      "result": "passed"
    }
  ],
  "platforms": ["linux-x86_64", "windows-amd64"],
  "limitations": [],
  "verified_at": "2026-08-19T00:00:00Z"
}
```

Required fields:

- `schema`, `requirement_id`, `revision`, `status`, and `claim`;
- exact baseline and verified Git SHAs for `verified` records;
- at least one evidence item for `verified`;
- platform/environment qualification when behavior is platform-sensitive;
- explicit limitations and checks not run.

Allowed statuses:

- `planned`
- `in_progress`
- `blocked`
- `implemented_unverified`
- `verified`
- `regressed`
- `superseded`
- `rejected`

Only `verified` permits `[x]` in the master spec.

## Evidence kinds

- `test`: exact node ID and result.
- `command`: command, interpreter/tool version, exit status, and bounded output artifact.
- `inspection`: deterministic inventory or source-query artifact.
- `benchmark`: harness, inputs, hardware/model identity, before/after values.
- `migration`: source state, backup, adoption, fault injection, and restore record.
- `replay`: source event range, replay version, equivalence assertion.
- `security`: abuse case, policy decision, containment/redaction evidence.
- `manual`: exceptional platform/user-interface verification with reviewer and reason no
  deterministic alternative exists.
- `external`: direct immutable reference to CI, PR, release, or signed artifact.

## Checker behavior

A future documentation-only checker should:

1. Parse every master-spec requirement ID and checkbox state.
2. Reject malformed or duplicate IDs.
3. Parse every JSONL record with bounded line length and strict keys.
4. Reject unknown IDs, non-monotonic revisions, duplicate latest revisions, and invalid
   status transitions.
5. Require a latest `verified` record for every checked requirement.
6. Reject a verified record whose Git SHA is absent from repository history.
7. Verify referenced in-repository evidence paths exist.
8. Require limitations/not-run disclosures for platform matrices.
9. Generate status by family, work package, and overall definition of done.
10. Never execute arbitrary commands embedded in the evidence ledger.

The checker validates evidence structure and linkage; it does not replace the tests or
prove that a claim is substantively correct.

## PR workflow

Every implementation PR must:

- Name its requirement IDs in the PR body.
- Capture the pre-change baseline.
- Add/update tests before checking a requirement.
- Append evidence records only after validation passes.
- Update focused documentation and generated status.
- State checks not run and why.
- Keep partially implemented requirements unchecked with
  `implemented_unverified` evidence.
- Mark newly observed regressions as `regressed` instead of preserving a stale checkmark.

## Initial adoption sequence

- [x] Commit the consolidated documentation and WP0 preparation artifacts.
- [x] Add the `requirements.jsonl` ledger and a JSON Schema.
- [x] Add a documentation checker that runs without importing production code.
- [x] Seed `planned` records for all 204 IDs from the master spec.
- [ ] Add CI freshness checks for generated status.
- [ ] Begin checking implementation requirements only as complete evidence is gathered.

This sequencing avoids retroactively treating existing code presence as proof.
