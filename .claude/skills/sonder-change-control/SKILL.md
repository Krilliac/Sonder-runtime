---
name: sonder-change-control
description: >-
  Non-negotiable change-control rules for the Sonder Runtime repo: how a change is
  classified by blast radius, which gates it must clear, and what must never be
  committed. TRIGGER when the user says "before I push", "open a PR", "is this safe
  to commit", "CI is red", "the ratchet failed", "regenerate the baseline", "revert
  this feature", or is about to commit, merge, tag a release, or touch a protected
  control-plane file. DO NOT TRIGGER for designing layering or import boundaries
  (use sonder-architecture-contract), for writing or judging tests and evidence
  quality (use sonder-validation-and-qa), or for running the selfmod pipeline
  itself (use sonder-selfmod-lifecycle).
---

# Sonder change control

How changes are classified, gated, and reviewed in the Sonder Runtime repository.
Single maintainer (Krilliac), ~2,000 commits mined 2026-07..08, 772 entries under
`tests/`. Every rule below exists because something broke; each rule states the
incident behind it.

**Vocabulary used below**

| Term | Meaning in this repo |
| --- | --- |
| Ratchet | A checked-in baseline that may only shrink. New findings fail CI; regenerating the baseline to go green is forbidden. |
| Evidence ledger | The checklist in `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md`, governed by `docs/architecture/EVIDENCE-TRACKING-DESIGN.md`. |
| Control plane | Files matched by `selfmod.protected_paths()` — approval, backup, rollback, permission, credential, and audit machinery. |
| Pinning test | A test that fails without the fix and passes with it — proves the fix is load-bearing. |

## When NOT to use this skill

- Deciding *where* code should live, import direction, or layer boundaries → `sonder-architecture-contract`.
- Writing tests, judging whether evidence is sufficient, pytest markers/flake policy → `sonder-validation-and-qa`.
- Privacy classifier internals, secret handling design, threat model → `sonder-security-and-privacy`.
- Doc tone, catalog generation, README conventions → `sonder-docs-and-writing`.
- Running, recovering, or budgeting an automated self-modification run → `sonder-selfmod-lifecycle`.

This skill is the *process* layer: classification, gates, commit/PR mechanics, and
the hard "never" list.

## 1. Classify the change first (blast-radius ladder)

Every change lands in exactly one tier. Higher tiers include all lower-tier gates.

| Tier | Change type | Extra gate beyond the tier below |
| --- | --- | --- |
| 0 | Docs-only | `git diff --check` (whitespace); evidence-ledger rules still apply if you touch checkboxes (§3) |
| 1 | Test-only | Full suite green; the new test must fail without the behavior it pins (prove it, don't assert it) |
| 2 | Behavior change | Add the test that would have caught the bug, or state in the PR why you cannot — that statement is required, not optional (CONTRIBUTING.md) |
| 3 | New tool surface / new dependency / security-posture change / large refactor | **Issue first.** "A 2000-line PR that arrives unannounced is hard to review honestly" (CONTRIBUTING.md). Capability-expanding surfaces that go wrong get *wholly reverted*, not patched (§5) |
| 4 | Protected control-plane files | Automatic (agent/selfmod) edits are forbidden. Human-reviewed change with explicit maintainer intent only. See §6 |

Check which tier a path is in:

```bash
python -c "import selfmod; import json; print(json.dumps(selfmod.protected_paths(), indent=2))"
```

Verified 2026-08-22: returns 26 protected prefixes and 10 protected
substrings. The full list and its per-entry rationale are owned by
`sonder-selfmod-lifecycle`; run the command above for the live set.

## 2. The CI gates, in order (each step gates the next)

Single `tests` job on `ubuntu-latest`, Python 3.12 (`.github/workflows/ci.yml`).
Runs on push to `main`, on every PR, and on manual dispatch. Reproduce each gate
locally before pushing:

| # | CI step | Local command |
| --- | --- | --- |
| 1 | Install deps | `python -m pip install -r requirements-dev.txt` |
| 2 | MCP-2 import probe | `python -c "from mcp.server.mcpserver import MCPServer; from mcp.server.mcpserver.tools import ToolManager"` |
| 3 | Architecture boundaries (SPEC-3) | `python scripts/check_architecture.py` |
| 4 | Evidence-ledger validation | `python scripts/check_requirement_evidence.py` |
| 5 | Error-signal ratchet (shrink-only) | `python scripts/check_error_signals.py` |
| 6 | Git-history privacy debt | `python scripts/check_history_privacy.py --json` |
| 7 | Test suite | `python -m pytest -q -n 2 --dist load --durations=20` |

Notes:

- The MCP probe exists because the runtime pins a specific `mcp` server API shape;
  a dependency bump that silently changes it must die at step 2, not deep inside
  the suite.
- `pytest.ini` sets `testpaths = tests proposals` — proposal modules ship in the
  desktop payload, and omitting them once let their compatibility tests silently
  disappear from bare CI runs (comment in `pytest.ini`). Do not narrow `testpaths`.
- There is **no lint gate**. No ruff/flake8 config exists in the repo
  (`ruff_verifier.py` at the root is a runtime verifier backend, not a project
  gate). The de-facto hygiene gate is `git diff --check`. Do not add a linter "to
  be helpful" — that is a tier-3 posture change; raise an issue.

### Release-only extra gates

`build-apps.yml` (jobs: `analyze`, `android`, `linux`, `windows`, `macos`,
`integrity`, `release`) builds the Flutter apps; per the maintainer these job names
are the required branch-protection checks (branch-protection settings are not
verifiable from the repo tree). The `release` job fires only on tags matching
`app-v*` and additionally requires:

```bash
python scripts/check_release_version.py --require-release --json
python scripts/check_history_privacy.py --require-clean --json
```

Note the asymmetry: ordinary CI runs `check_history_privacy.py --json` (prevent
*new* debt); releases run `--require-clean` (zero debt). A change that adds
history-privacy debt can pass PR CI and still block the next release — treat the
stricter flag as the real bar. The authoritative version is
`sonder_version.VERSION` (currently `0.9.0.dev0`, 2026-08-22); the `integrity` job
reads it directly, so version bumps go there, nowhere else.

## 3. Evidence-ledger rules (checkbox discipline)

Source: `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md` §1 and
`docs/architecture/EVIDENCE-TRACKING-DESIGN.md`. Enforced by
`scripts/check_requirement_evidence.py` in CI (gate 4).

- `[ ]` means **unverified**, even if the code already exists. A feature is not
  complete because a class, test double, proposal, or legacy path exists.
- `[x]` requires committed evidence with exact 40-hex `baseline_sha` and
  `verified_sha` Git SHAs; a verified record whose SHA is absent from repository
  history is rejected.
- Requirement IDs (`CORE-001`, `ARCH-010`, ...) are permanent. Never renumber an
  existing ID to improve presentation.
- A regression flips status to `regressed`. Never leave a stale checkmark — a
  checkmark that stopped being true is a check that stopped checking.
- Deleting obsolete behavior is part of completion, not optional cleanup.
- Documentation-only completion never proves runtime completion.

Rationale: with a single maintainer and agent fleets writing code, the ledger is
the only durable memory of what was actually proven versus merely written. A
checkbox you can tick without evidence is a checkbox that will be ticked wrongly.

## 4. The ratchet doctrine (the most-violated rule — do not violate it)

`scripts/check_error_signals.py` tracks two exact categories of legacy stringly
error signaling (`return` of a literal-derived `ERROR:` prefix, and
`.startswith("ERROR:")` parsing) against `scripts/error_signal_baseline.json`.
The baseline is an upper-bound universe: findings may disappear; **a new scope,
category, or additional occurrence fails CI**.

When the ratchet goes RED:

1. **Fix the site.** Migrate the new `ERROR:` string to a structured error, or
   remove it. Do not add, and do not swap one site for another.
2. **Measure the consequence of bypass** before claiming your pinning test is
   load-bearing: disable the guarded branch and show the bad behavior actually
   occurs.
3. **NEVER regenerate the baseline to go green.**

Historical incident (the exemplar — read it): commit `2cec3272` added one new
stringly `ERROR:` return in `_agent_dispatch` as part of a root-confinement
fix, and the ratchet went RED; commit `1ab0038b` (2026-08-11) is the fix.
The fix reproduced the failure unpiped
(exit 1, one finding at `server.py:14557`), gave the refusal a structured gate
that owns it, and recorded in the commit body why regeneration was refused:

> "The baseline was NOT regenerated. ... regenerating is the one-line change that
> makes the red vanish and permanently widens it."

The same commit demonstrates bypass measurement: with only the guarded branch
disabled, a read-only agent successfully ran `secret_scan` — "Measured, not
assumed." That is the standard a RED-ratchet fix must meet.

```bash
git show 1ab0038b --stat        # read the full exemplar
```

## 5. Capability-expanding surfaces: revert whole, patch never

In ~2,000 commits there is exactly one wholesale revert: `ae9503b0` (2026-08-08)
reverted the entire `shell_run` tool — a new arbitrary-shell-execution surface —
rather than hardening it in place. The rule this encodes:

- A tool surface that expands what the runtime can *do* (execute shells, write
  outside the root, reach the network) and turns out unsafe is **fully reverted**,
  then redesigned via an issue. Patching a live unsafe capability leaves the
  capability live while you argue about the patch.
- This is why tier 3 is issue-first: the cheap moment to say "no" is before the
  surface exists.

```bash
git show ae9503b0 --stat        # the revert; note it removes the surface entirely
```

## 6. Protected control-plane files (tier 4)

`selfmod.protected_paths()` (in `selfmod.py`, `SENSITIVE_PREFIXES` /
`SENSITIVE_PARTS` near the top of the file) defines paths where automatic edits
are forbidden: permission and approval logic, backup/rollback (`safe_update.py`),
credentials, audit trails, the selfmod machinery itself, and the tests that pin
all of the above (`tests/test_permission*`, `tests/test_selfmod*`,
`tests/test_control_plane*`, `tests/test_read_only_agent_policy*`).

Rationale: the runtime modifies its own code (see `sonder-selfmod-lifecycle`).
The one thing a self-modifying system must never be able to modify automatically
is the machinery that constrains self-modification — otherwise every other gate in
this document is one automated edit away from deletion. Protecting the *tests*
too closes the obvious loophole of weakening a guard by weakening its pin.

If your change legitimately needs to touch a protected path: it is a
human-reviewed, maintainer-intent change. Say so explicitly in the commit body
and PR; never let it ride along inside an unrelated diff.

## 7. Never commit (privacy hard lines)

From CONTRIBUTING.md, all gitignored for exactly this reason:

- **Personal training data or adapters trained on it**: `personal_dataset.jsonl`,
  `combined_personal.jsonl`, `sonder-personal-lora/`, `sonder-personal-merged/`,
  `Modelfile.personal`. Model weights can memorize training data; publishing an
  adapter fitted to a private codebase can leak the codebase.
- **`memory.db` or any unscrubbed derivative** — it holds raw prompts and
  responses. To share learned lessons, use the opt-in export, which runs every
  row through the privacy classifier, then *read the output before attaching it*:

  ```bash
  venv/Scripts/python contribute.py     # -> contrib/lessons_contrib.jsonl
  ```

- **Credentials of any kind, including in test fixtures.** Use obviously fake
  values; existing tests use AWS's published example key style.

Historical incidents recorded as comments in `.gitignore` itself (read them —
they are the institutional memory):

- The runtime virtualenv moved from `venv/` to `.runtime/` but `.gitignore` did
  not follow, leaving **83 MB of installed packages** one `git add -A` away from
  being committed.
- `.claude/` holds git worktrees — a second full copy of the repo — which made
  repo-wide source scans report the copy's files as offenders until ignored.

CI gate 6 (`check_history_privacy.py`) exists because a privacy mistake that
reaches Git history is nearly irreversible; the gate prevents *new* debt on every
PR and the release gate requires zero debt (§2).

## 8. Git conventions (mined from history, 2026-07..08)

Commit subject prefixes, by observed frequency in the last 2,000 commits
(2026-08-22): `fix:` (304), `feat:` (136), `chore:` (58), `docs:` (28),
`selfmod:` (24), `test:` (21), `refactor:` (15). Sentence-case imperative
subjects.

Branch namespaces (counted 2026-08-22; the count depends on the method):
remote-only (`git branch -r`) — `agent/*` 99, `claude/*` 0, `codex/*` 10;
including local branches and worktree checkouts — approximately 108, 20, 22.
Plus `improve/*` and `selfmod/<run-id>`.

Merge style: PR-based merges for reviewed work; direct merges to `main` are
*observed maintainer practice* for agent-fleet integration branches, not a
license — external contributions go through a PR with the full suite green,
per CONTRIBUTING.md.

**The commit body is where change control lives.** The best commits in this
history carry: the reproduction, exact `file:line`, what was measured versus
assumed, and why the obvious shortcut was refused (see `1ab0038b` for the
canonical example). CONTRIBUTING.md's phrasing is the bar: *"say what you
verified, not what you believe"* — "Reproduced the failure, fixed it, the new
test fails without the fix" beats a paragraph of description.

`.gitignore` line 39 ignores `.claude/`, so skills and agent config under
`.claude/` require `git add -f` to stage — a plain `git add` silently stages
nothing, which looks exactly like success (a check that stops checking).

## 9. Before you push — checklist

Run from the repo root. Every command below was executed against this tree.

```bash
# 1. Compile everything you could have broken (fast, catches syntax/import rot)
python -m compileall -q sonder_runtime tests        # exit 0, silent when clean

# 2. Whitespace gate (the de-facto lint)
git diff --check                                    # exit 0, silent when clean

# 3. The cheap CI gates, locally, in CI order
python -c "from mcp.server.mcpserver import MCPServer; from mcp.server.mcpserver.tools import ToolManager"
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python scripts/check_error_signals.py
python scripts/check_history_privacy.py --json

# 4. Focused tests for what you touched, THEN the full suite
python -m pytest -q tests/<the-tests-that-pin-your-change>
python -m pytest -q -n 2 --dist load --durations=20
```

**Known-red baseline at 99162cf9** (verified by execution, 2026-08-23): a bare
full suite at that commit fails exactly two tests —
`tests/test_remaining_doc_001_005.py::test_authority_checker_passes_and_inventory_is_complete`
and `::test_public_generator_freshness_check_passes` — because four generated
catalog files (`runtime-reference.json`/`.md`, `architecture-map.json`/`.md`)
are stale; `scripts/check_documentation_authority.py` exits 1 for the same
reason. If those two are your only reds, they are the baseline, not your
change. Remedy (production-scope):
`python scripts/generate_documentation_catalogs.py --write` — see
`sonder-docs-and-writing`.

Then, in the commit/PR, state — precisely, not optimistically:

- [ ] What you **verified** (commands + observed output), not what you believe.
- [ ] The pinning test, and proof it fails without the fix — or the explicit
      statement of why no such test exists.
- [ ] Tier (§1) and, for tier ≥ 3, the issue it was discussed in.
- [ ] For any checkbox change: the evidence SHAs (§3).
- [ ] Confirmation nothing from the never-commit list (§7) is in the diff:
      `git diff --cached --stat` and read it.

A clean run of steps 1–3 with an empty diff would look identical to a clean run
with your change — before believing green, confirm the diff is actually staged
and the tests you cite actually collected (>0 tests run).

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All commands above were executed
or Read-verified against that tree. Re-verify volatile facts:

- CI gate order: `cat .github/workflows/ci.yml`
- Release gates and job names: `grep -nE "^  [a-z-]+:|require-release|require-clean" .github/workflows/build-apps.yml`
- Protected paths: `python -c "import selfmod, json; print(json.dumps(selfmod.protected_paths(), indent=2))"`
- Ratchet semantics: `sed -n '1,10p' scripts/check_error_signals.py` (docstring states shrink-only)
- Exemplar commits still resolve: `git show --stat 1ab0038b ae9503b0`
- Checkbox rules: `sed -n '26,34p' docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md`
- `.claude/` still gitignored: `grep -n claude .gitignore`
- No lint config appeared: `ls pyproject.toml ruff.toml .ruff.toml 2>/dev/null` (all absent as of 2026-08-22)
- Version identity: `python -c "import sonder_version; print(sonder_version.VERSION)"` (`0.9.0.dev0` as of 2026-08-22)
- Prefix/branch frequencies: `git log --oneline -2000 --format='%s' | grep -oE '^[a-z]+:' | sort | uniq -c | sort -rn`
