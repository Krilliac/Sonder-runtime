# Plan 02 — Make calibration load-bearing

## Context

`calibration.py` and `grounded_outcomes.py` landed in `5c7b4e6`. Both work and
are tested. Neither yet changes anything.

- `calibration.should_verify(conn, population)` returns `(bool, reason)` and is
  called by nothing but its own tests. It was built precisely so that measured
  reliability would *gate* rather than decorate — a confidence figure that only
  garnishes a reply is theatre.
- `grounded_outcomes` is hooked into `_record_direct_tool`, which covers direct
  MCP tool calls only. The agent loop, `workbench_agent`, and autopilot generate
  and verify far more work than direct calls do, and that is where the outcome
  imbalance actually comes from: the store holds **8,883** self-graded
  `tests_passed` rows against **192** caller-judged ones (101 good / 91
  rejected = 52.6%).

## Global Constraints

- **Never average the two populations.** A single figure over both reads like
  accuracy and is not one. There is deliberately no `overall()` in `calibration`
  and a test asserts it does not exist. Do not add one, in any surface.
- **Attribution must keep failing toward silence.** A wrong link poisons the
  very population this exists to clean up. Bounded by time, project, and one
  verdict per generation per verification kind — do not relax any of these to
  raise the capture rate.
- **Gating must be honest about ignorance.** Below `MIN_SAMPLE`, verification is
  demanded, not skipped. Do not add a "probably fine" path.
- Every behaviour change needs a test that fails before the change.
- `python scripts/check_error_signals.py` must stay silent (CI ratchet).
- Full suite must stay green: currently **5612 passed, 46 skipped**.

## Task 1 — Attribute outcomes from the agent loop

Extend `grounded_outcomes` capture to the agent/workbench/autopilot paths, which
currently file nothing.

- Find where those loops invoke tools and complete work (`_agent_dispatch` and
  the workbench loop are the likely seams).
- A generation inside an agent loop followed by a verification inside the same
  loop is the strongest possible link — stronger than the direct-call case,
  because both are inside one bounded run. Use the run/span identity rather than
  only the time window where one is available.
- Do NOT double-file: a tool call that already routed through
  `_record_direct_tool` must not be attributed twice.

Tests: an agent run that generates then fails a build files exactly one
`failed`; the same run does not file twice; a run with no verification files
nothing.

## Task 2 — Close the advertised-but-not-callable gap

**Added mid-plan, and it blocks the task after it.** Task 1's implementer
noticed that `_agent_dispatch` has no branch for the verification tools. That
turned out to understate it. Measured on this branch:

- `AGENT_TOOL_HELP` advertises **130** tools to the agent.
- `_agent_dispatch` can actually call **117**.
- **23 are advertised and not callable**: `test_run`, `test_discover`,
  `build_run`, `build_clean`, `lint_run`, `format_code`, `typecheck_run`,
  `secret_scan`, `diff_files`, `find_references`, `rename_symbol`,
  `apply_patch`, the whole `dependency_*` family, and every `git_*` tool.

These are the 25 developer-workflow tools added in an earlier session. They were
put into `_WORK_INSPECTION_TOOLS`, `_AUTOPILOT_WORKSPACE_TOOLS` and the help
text, but never given dispatch branches. So the agent is told in its own tool
help that it can run the tests, and cannot. This is a shipped defect on `main`,
introduced by the controller, not by any implementer here.

It blocks Task 3: gating a completion claim on verification is meaningless when
the agent cannot invoke a verifier.

Add the missing `_agent_dispatch` branches so every advertised tool is callable.

- Follow the shape of the existing branches exactly, including how they read
  arguments and pass `token`/`approval`/`extra_roots` where the tool takes them.
- Mutating tools among these (`git_commit`, `apply_patch`, `rename_symbol`,
  `dependency_*`, `build_clean`) must keep whatever gating already applies to
  them via `_agent_tool_mutates` and the policy sets — making a tool *reachable*
  must not make it *ungated*. Check `_agent_tool_mutates` handles each one.
- **Add a drift test** asserting that every name advertised in
  `AGENT_TOOL_HELP` is dispatchable, so this cannot silently recur. That test is
  the real deliverable — the branches are mechanical, the invariant is not.

Tests: the drift test above (it must fail before your fix — verify that); a
representative dispatch for a read-only tool and for a mutating one; the
mutating one still respects its existing approval/dry-run gate.

## Task 3 — Gate completion claims on measured reliability

Wire `should_verify()` into the place where work is declared done.

- The agent loop already has an end-report and a validation gate. When
  `should_verify()` is true, an agent must not report success on the strength of
  its own say-so — it must cite a verification (a test/build/lint result) or
  report the work as unverified.
- The wording must be measured, never generated: surface `should_verify`'s own
  `reason` string, which is a projection of counts.
- This is the crux of the plan. If it cannot be done without weakening the end
  report, stop and report BLOCKED rather than adding a decorative field.

Tests: with a poor/unmeasured record, an agent run that produced no verification
is reported unverified; with a good record it is not; the reason text contains
the measured counts.

## Task 4 — Expose calibration where decisions are made

`calibration_status` exists as an MCP tool. Add the two surfaces that make it
actionable:

- Include the measured caller-judged figure in `learning_health_status` output,
  clearly separated from the curriculum figure (that report already keeps the
  populations apart — match its existing framing rather than inventing new).
- Add the `should_verify` verdict to the agent end-report header, so a caller
  reading a report can see the standing the claim was made under.

Tests: both surfaces show the populations separately; neither prints a combined
percentage.
