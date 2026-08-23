---
name: sonder-docs-and-writing
description: >-
  Writing and maintaining the Sonder Runtime docs of record: where each kind
  of fact goes, the ADR/evidence/runbook/wiki templates, the checkers that
  gate them, and the house style. TRIGGER when asked to "update the docs",
  "write an ADR", "add a runbook", "document this change", "add a wiki page",
  "record evidence for a requirement", "regenerate the catalogs", or draft
  "release notes" (venue and format). DO NOT TRIGGER for deciding which
  document is authoritative or citing requirement status — those rules live in
  sonder-architecture-contract; for what a release note or public claim may
  ASSERT and the evidence it requires, use sonder-external-positioning. This
  skill is about producing and maintaining the writing itself.
---

# Sonder docs and writing

Sonder Runtime is a docs-heavy repo: 600+ markdown files, a generated catalog
set, an append-only evidence ledger, two ADR namespaces, 20 runbooks, and a
19-page wiki. Every kind of fact has exactly one home, and several checkers
fail the build when documents drift. This skill is the runbook for adding or
changing any of that writing without tripping a gate or polluting a namespace.

**When NOT to use this skill.** If you need to know which document wins a
conflict, or whether you may cite a doc as proof of current status, use
`sonder-architecture-contract` (it owns the authority rules; this skill only
applies them). If you are debugging why a CI gate failed on a non-doc change,
use `sonder-change-control`. If you are writing code comments as part of a
bug fix, the one rule you need is in the house-style section below.

Terms used once and defined once:

- **ADR** (Architecture Decision Record): a short dated file recording one
  irreversible-ish decision and its consequences.
- **Evidence document**: a dated markdown record in
  `docs/architecture/evidence/` containing exact commands and observed output.
- **Evidence ledger**: `docs/architecture/evidence/requirements.jsonl`, an
  append-only JSON-lines file mapping master-spec requirement IDs to status.
- **Generated catalogs**: files under `docs/architecture/generated/` emitted
  by scripts; hand-editing them is always wrong.

## 1. Where a new fact goes (the taxonomy)

One home per fact. Before writing, find the row that matches what you know:

| You have... | It goes in | Notes |
|---|---|---|
| A current product/boundary contract | `ARCHITECTURE.md`, `SECURITY.md`, `SELFMOD.md`, `TRAINING.md`, `CLIENT.md`, or `MOBILE_HOST_CONTROL.md` | The six focused contracts listed in `docs/architecture/DOCUMENT-AUTHORITY-INDEX.md` |
| A requirement's checkbox state | `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md` | Only with a matching `verified` ledger record (section 5) |
| Proof that something was verified | `docs/architecture/evidence/<ID>-<DESCRIPTION>-<YYYY-MM-DD>.md` | Section 6 |
| An architectural decision | `docs/adr/ADR-YYYY-MM-DD-<slug>.md` | Section 3. Never the numeric series |
| An operational procedure | `docs/runbooks/<slug>.md` + index entry | Section 7 |
| Conceptual/user documentation | `docs/wiki/NN-<slug>.md` + index entry | Section 8 |
| A forensic incident/audit report | `.superpowers/sdd/work/<slug>-report.md` | Section 9 shape |
| First-contact overview / install | `README.md` | The front door; keep claims matched to code |

Two hard prohibitions from the authority index (verify with
`python scripts/check_documentation_authority.py`):

1. **Never edit historical docs to claim current status.**
   `docs/architecture/PROGRAM-STATUS.md`, `SPEC-5-*`, `WP0`–`WP9` slice docs,
   `REMAINING-*.md`, and `REQUIREMENT-AUDIT-NEXT.md` are retained history.
   The checker requires the historical set to keep a "superseded" or
   "historical snapshot" label in their first 14 lines — removing the label
   fails the gate.
2. **Never duplicate a fact across homes.** If a change makes a second doc
   describe the same behavior, one of them should link to the other instead.

## 2. The checkers that gate docs

Four scripts gate documentation. All are read-only unless you pass a write
flag. Run from the repo root:

| Gate | Command | What it rejects | Where it runs |
|---|---|---|---|
| Authority + ADR + freshness | `python scripts/check_documentation_authority.py` | Missing historical labels, missing focused contracts, bad ADR filenames, duplicate ADR identifiers, stale generated catalogs | pytest, via `tests/test_remaining_doc_001_005.py` and `tests/test_document_authority.py` |
| Generated catalogs only | `python scripts/generate_documentation_catalogs.py --check` | Any generated file that differs from regeneration | Same freshness comparison as above (shared `expected()` mapping) |
| REMAINING-doc discipline | `python scripts/check_evidence_documents.py` | A `docs/architecture/REMAINING-*.md` with no limitation/evidence/verification/coverage disclosure, no backticked `tests/...py` reference, or a reference to a missing test file | pytest, via `tests/test_evidence_document_consistency.py` |
| Requirement ledger | `python scripts/check_requirement_evidence.py` | Malformed ledger records, checked boxes without `verified` evidence, stale status projections | Named CI step in `.github/workflows/ci.yml` AND pytest |

Only the ledger gate is a named CI step; the other three reach CI through the
test suite. All four are also documented as required PASS gates in
`docs/architecture/evidence/EVIDENCE-ROADMAP-2026-08-21.md`.

Known-failing at the verified commit (2026-08-22): the authority gate reports
four stale generated files (`runtime-reference.*`, `architecture-map.*`)
because the multi-PC worker merge added an `ollama.workers` configuration
field without regenerating. On the pytest side the same staleness fails
exactly two tests:
`tests/test_remaining_doc_001_005.py::test_authority_checker_passes_and_inventory_is_complete`
and
`tests/test_remaining_doc_001_005.py::test_public_generator_freshness_check_passes`
(the known-red baseline noted in `sonder-change-control` §9 and
`sonder-validation-and-qa`). The fix is the regeneration command in
section 4, committed alongside nothing else hand-edited.

## 3. Writing an ADR

Namespace rules (enforced by `check_documentation_authority.py`):

- New ADRs go in `docs/adr/` named `ADR-YYYY-MM-DD-<slug>.md`. The date plus
  slug is the globally unique identifier. The slug must be lowercase
  `[a-z0-9-]` — the checker regex is
  `^ADR-\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.md$`.
- Two historical numeric namespaces exist and are frozen: `ADR-001`–`006` in
  `docs/adr/` (SPEC-5 era) and `ADR-001`–`009` in `docs/architecture/adr/`
  (modular-monolith era). Numbers are never reused, renumbered, or extended.
  The reused numbers across the two directories do not collide because
  neither is the namespace for new decisions.
- Supersession: a newer date-prefixed ADR or the master spec may explicitly
  supersede an older decision; until then, cite the older path and state its
  historical scope (`docs/architecture/adr/README.md`).

Template — reproduce the skeleton of the existing records (e.g.
`docs/adr/ADR-004-transactional-outbox.md`):

```markdown
# ADR-2026-08-22-<slug>: <Title>

**Status:** Accepted
**Date:** 2026-08-22
**Context:** <spec section / requirement IDs / triggering incident>

## Decision

<One paragraph. What is now true.>

## Rationale

<Why, especially why the obvious alternative loses.>

## Consequences

- <What each affected component gains or must now do>
- <What is explicitly ruled out>
```

Note: as of 2026-08-22 no date-prefixed ADR exists yet in `docs/adr/`; the
first one you write establishes the concrete precedent, so match the numeric
records' section shape exactly.

## 4. Generated files: regenerate, never hand-edit

Everything under `docs/architecture/generated/` says so itself
("Generated by `scripts/generate_documentation_catalogs.py`; do not edit
manually"). The full set and its regeneration:

| Files | Regenerate with |
|---|---|
| `runtime-reference.{json,md}`, `architecture-map.{json,md}`, `focused-contract-inventory.{json,md}` | `python scripts/generate_documentation_catalogs.py --write` |
| `requirement-status.{json,md}` | `python scripts/check_requirement_evidence.py --write-generated` |

Then confirm freshness before committing:

```bash
python scripts/generate_documentation_catalogs.py --check
python scripts/check_requirement_evidence.py
```

Properties you rely on (from the generator source): SHA-256 digests are
computed over LF-normalized text so Windows CRLF checkouts and CI produce
identical output; a changed typed source must change the digest, a reordered
source must not; oversized output fails closed.

`docs/architecture/migration-inventory.json` is also an output, per the
master spec ("it must be regenerated and eventually prove zero legacy
exceptions"). No in-repo script was found that writes it (open: its
regeneration path); treat it as a dated snapshot — do not hand-edit values.

## 5. The evidence ledger and checkbox discipline

`docs/architecture/evidence/requirements.jsonl` is append-only JSON lines,
one record per status change. `check_requirement_evidence.py` enforces
(verified against the script at the pinned commit):

- Required keys: `schema` (`sonder-requirement-evidence-v1`),
  `requirement_id`, `revision`, `status`, `claim`. Allowed extras:
  `baseline_sha`, `verified_sha`, `pr`, `evidence`, `platforms`,
  `limitations`, `verified_at`. Unknown keys are rejected.
- Valid statuses: `planned`, `in_progress`, `blocked`,
  `implemented_unverified`, `verified`, `regressed`, `superseded`,
  `rejected`.
- Revisions per requirement must be strictly increasing — you append a new
  record, never rewrite an old one. Lines over 16,384 characters are
  rejected. The checker parses the ledger; it never executes anything
  embedded in it.
- **A master-spec checkbox may be `[x]` only if the latest ledger record for
  that ID is `verified`**, and a `verified` record must carry
  `baseline_sha`, `verified_sha`, and `evidence`. A regression is a new
  appended record with status `regressed` — not an edit, not a deletion.

Baseline for calibration (from the 2026-08-21 roadmap checkpoint): 204
requirements, 0 checked, 0 verified, all latest records
`implemented_unverified`. Promotion to `verified` is deliberately rare and
happens in its own reviewed change.

## 6. Evidence documents

`docs/architecture/evidence/` holds 126 dated `.md` records (counted
2026-08-22; 128 directory entries including the schema JSON and
`requirements.jsonl`). Observed naming
convention (not machine-enforced): `<FAMILY>-<NNN>-<DESCRIPTION>-<YYYY-MM-DD>.md`
when tied to a requirement ID (`EXT-003-BOUNDED-EXTENSION-HOST-2026-08-21.md`),
or `<TOPIC>-<YYYY-MM-DD>.md` otherwise (`EVIDENCE-ROADMAP-2026-08-21.md`).

Required content, modeled on the existing records:

1. **Scope** — what changed, with exact paths.
2. **Verification** — the exact command(s) and the observed output/result
   ("Result: **9 passed, 1 skipped**"), including the temp/basetemp used if
   isolation matters.
3. **Limitations** — what this evidence deliberately does not claim. Every
   good evidence doc in this repo has one; an evidence doc without a
   limitations section is overselling.

An interrupted run is not evidence — the roadmap checkpoint explicitly
records an interrupted pytest batch as "no test-pass/fail conclusion and must
not be used as evidence". Say what a run proved, or say it proved nothing.

## 7. Runbooks

`docs/runbooks/` contains 20 procedures plus `README.md`. Adding one:

1. Create `docs/runbooks/<slug>.md` following the observed shape (e.g.
   `ollama-outage.md`): **Symptoms** (what the operator sees, with exact
   endpoints/metrics), **Diagnosis** (copy-pasteable commands), **Recovery**
   (numbered steps), **Aftermath** (what to check afterward). State
   preconditions when they differ from the default: runbooks assume the
   server-private reference deployment (systemd, loopback bind, reverse
   proxy) unless the runbook says otherwise.
2. Add a line to `docs/runbooks/README.md` — the index is a flat link list,
   with a trailing `— <clarifier>` only when the slug alone is ambiguous.
3. Every command must be one the operator can paste. Expected observations
   belong next to the command ("`/ready` flips back to 200 without a Sonder
   restart"), not in a separate appendix.

## 8. Wiki pages

`docs/wiki/` is 19 numbered pages (`01-architecture.md` …
`19-model-requirements-and-onboarding.md`) plus `README.md`, whose Map table
lists every page with a one-line summary. Adding page `20-<slug>.md` means:

1. Take the next free number; never renumber existing pages (inbound links).
2. Add the row to the Map table in `docs/wiki/README.md`.
3. Wiki pages are conceptual ("what it is, how it works"); operational
   step-by-steps belong in a runbook, which the wiki links to.

## 9. Incident and audit reports

Forensic reports live in `.superpowers/sdd/work/`. The house shape, observed
across the existing reports (e.g. `smoke-gate-report.md`):

symptom → mechanism → evidence → fix → proof the fix can fail.

Concretely: open with lineage verified rather than assumed (the observed
reports literally run `git merge-base --is-ancestor` and show the output);
quote the offending code with a `file:line` anchor; re-resolve anchors
against the current tree before publishing, because they drift; close each
finding with the test or probe that fails when the fix is reverted.

## 10. README maintenance

- The badge block between `<!-- ci-artifact-badges:start -->` and
  `<!-- ci-artifact-badges:end -->` (README lines 14–20 at the pinned
  commit) is marker-delimited for tooling — edit nothing inside it by hand.
- The README says it itself: the `app-latest` badges are a **mutable
  prerelease snapshot**, may lag `main`, and are not a versioned,
  release-ready build. Never describe them as a release.
- WP1 slice notes are appended at the bottom, after the "Security and
  contributing" section. The dominant convention — 80 entries at the pinned
  commit — is a single-line bullet:
  `- WP1 <Nth> Slice: <what moved where>, preserving <compat surface>.`
  One outlier uses an `# WP1 ... Slice` heading with a bullet under it, and
  one entry is indented as a sub-bullet. Follow the dominant single-line
  form; do not reformat the outliers in an unrelated change.
- There is no dedicated release-notes file in this repo (open: whether one
  should exist). Release-facing writing lives in
  `docs/runbooks/publish-release.md` (the TUF signing ceremony),
  `docs/runbooks/release-version-policy.md`, and the README quick-start
  wording above. If asked for "release notes", draft them from the WP1 slice
  notes and merged-commit subjects, and label them draft until a maintainer
  blesses a venue.

## 11. House style

From `CONTRIBUTING.md` and the observed corpus:

- **Comments explain WHY, especially when the obvious approach is wrong.**
  "If a fix is subtle, the comment explaining what bit you is part of the
  fix." Restating the code is noise.
- **Say what you verified, not what you believe.** "Reproduced the failure,
  fixed it, the new test fails without the fix" beats a paragraph of
  description. Static reasoning is never presented as a test result.
- **Failures made louder, not quieter.** "A probe that fails silently to a
  plausible-looking default has cost this project more than one bad
  afternoon." Documentation follows the same rule: document the failure mode
  and its observable symptom, not just the happy path.
- **Date-stamp volatile facts** (counts, versions, staleness claims) and
  qualify counts as floors or totals — the corpus writes "126 dated `.md`
  records (counted 2026-08-22)", "at least one", "0 of 204 checked", never a
  bare number that will silently rot.
- **A false README claim is a bug.** CONTRIBUTING lists "a correction to
  documentation that claims something the code does not do" as a merge-worthy
  contribution in its own right; when your code change falsifies a doc
  sentence, fix the sentence in the same change.
- **Never write bare debt-marker tokens** — the conventional all-caps words
  that grep-based debt scanners hunt for — into docs. The repo's discipline
  is to record open work as an explicit `open:` or `candidate:` label with an
  owner document, or as a `REMAINING-*` entry that names a focused test.

## 12. Pre-commit checklist for any doc change

```bash
python scripts/check_documentation_authority.py     # exit 0 required
python scripts/check_evidence_documents.py          # exit 0 required
python scripts/check_requirement_evidence.py        # exit 0 required
python scripts/generate_documentation_catalogs.py --check   # exit 0 required
git diff --check                                    # no whitespace damage
```

And the non-mechanical checks:

- [ ] The fact has exactly one home, and other docs link rather than repeat.
- [ ] No historical doc (`PROGRAM-STATUS`, `WP*-*`, `REMAINING-*`, `SPEC-5-*`)
      was edited to claim current status.
- [ ] No file under `docs/architecture/generated/` was hand-edited.
- [ ] No checkbox flipped without a `verified` ledger record appended first.
- [ ] New ADR/runbook/wiki files are reflected in their index
      (`docs/adr` needs no index; `docs/runbooks/README.md` and
      `docs/wiki/README.md` do).
- [ ] Volatile numbers are date-stamped; commands were actually run.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All gate commands in section 2
were executed at that commit; `check_evidence_documents.py` and
`check_requirement_evidence.py` exited 0, `check_documentation_authority.py`
exited 1 with the four stale catalog files noted in section 2.

Re-verify before trusting:

- Gates still exist and pass: `python scripts/check_documentation_authority.py && python scripts/check_evidence_documents.py && python scripts/check_requirement_evidence.py`
- Catalog freshness: `python scripts/generate_documentation_catalogs.py --check`
- CI step list: `grep -n "scripts/" .github/workflows/ci.yml`
- ADR namespace rules: `sed -n '1,25p' docs/architecture/adr/README.md`
- Authority map: `sed -n '1,60p' docs/architecture/DOCUMENT-AUTHORITY-INDEX.md`
- Wiki/runbook counts: `ls docs/wiki | wc -l` and `ls docs/runbooks | wc -l`
- WP1 slice-note dominant form count: `grep -c "^- WP1 .*Slice:" README.md`
- Ledger size and statuses: `wc -l docs/architecture/evidence/requirements.jsonl`
