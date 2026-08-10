# Plan 01 — Finish the permission gate

## Context

`permission_modes.py` (landed in `5c7b4e6`) supplies a decision point that did
not previously exist: it maps a tool's *risk class* to allow/ask/deny per mode.
But two things it was built to sit alongside are still inert:

1. **`permission_rules.check()` is never called anywhere.** The module holds
   per-tool `allow`/`ask`/`deny` pattern rules, `/permissions` prints them, and
   `permission_rule_set` writes them — but nothing consults them at dispatch.
   `/permissions` currently reports `file_delete: deny` while the only thing
   actually stopping a delete is that tool's own `dry_run` default. A policy
   that reports one thing and enforces another is worse than no policy: it
   invites reliance.

2. **`permission_modes.PRIVILEGED_TOOLS` is an empty frozenset.** The elevation
   branch in `decide()` is therefore unreachable in production. Elevation is
   modelled, tested, and gates nothing.

This plan makes both real without breaking flows that have always worked.

## Global Constraints

- **Preserve current behaviour by default.** These rules have been dormant since
  they were written. Switching them to fully enforcing in one step would break
  working flows. Mode `manual` (the default) must not start denying things that
  worked yesterday.
- **Per-tool rules and modes compose; they do not race.** Define and document
  the precedence explicitly. The intended rule: an explicit per-tool `deny`
  always wins; an explicit `allow` is a standing exemption that satisfies the
  mode's `ask`; `plan` mode's denials are never overridden by a rule, because
  holding still is that mode's entire purpose.
- **Fail closed on ignorance.** An unknown tool must never resolve to `safe`.
- Every behaviour change needs a test that fails before the change.
- `python scripts/check_error_signals.py` must stay silent — do not introduce
  any new `"ERROR: ..."` string literal (CI ratchet).
- The full suite (`python -m pytest tests/ -q`) must stay green: currently
  **5612 passed, 46 skipped**.

## Task 1 — Wire `permission_rules.check()` into the decision point

Make `permission_modes.decide()` consult the per-tool rules and combine them
with the mode matrix under the precedence above.

- Add rule lookup to `decide()`. Keep `decide()` pure and injectable — it must
  remain testable without touching the user's real rules file.
- Document the precedence in the module docstring, including *why* an explicit
  `deny` outranks `auto` (a standing, written-down exemption is a narrower and
  more auditable decision than a mode).
- `Decision.reason` must say which layer decided, so an operator can tell a
  mode refusal from a rule refusal.

Tests: rule `deny` beats every mode including `auto`; rule `allow` satisfies
`manual`'s `ask`; rule `allow` does NOT override `plan`'s deny; no rule falls
through to mode behaviour unchanged; unknown tool still fails closed.

## Task 2 — Populate `PRIVILEGED_TOOLS` and make elevation mean something

- Identify the tools that genuinely require OS administrator rights. Start from
  evidence, not guesswork: `dism` returned error 740 under `workspace_run`, so
  host-execution tools *can* need it, but the tool itself is not inherently
  privileged. Prefer marking a small, defensible set and documenting the
  criterion over a long speculative list.
- If the honest answer is that no *tool* is unconditionally privileged, say so
  in the module and instead make elevation gate the *capability*: e.g. a
  `requires_elevation` argument path, or a documented decision that the set
  stays empty with the reasoning recorded. **An empty set with a written
  rationale is an acceptable outcome for this task** — better than inventing
  privilege for tools that do not need it.
- Add `/elevate` handling: session-scoped, explicit, never set by a mode,
  never restored from disk (this invariant is already tested — keep it).

Tests: a privileged tool is denied when elevation is off and permitted when on;
no mode change alters elevation (already covered — extend if the set changes).

## Task 3 — Make `/permissions` tell the truth

`permission_policy` currently prints rules that were not enforced. Now that they
are, its output must reflect the *effective* decision: the rule, the active
mode, and which one governs for that tool.

Tests: output names the active mode; a tool whose rule and mode disagree shows
which wins.
