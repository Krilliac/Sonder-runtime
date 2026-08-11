# Defect #28 — "the app ships the raw permission matrix with no override channel"

Lane: `work/24-app-permissions` @ `5b3ae1a`. Worktree `D:\sonder-wt\24-app-permissions`.

## Lineage — verified, brief is correct

`git merge-base --is-ancestor` run against HEAD:

| claimed ancestor | result |
| --- | --- |
| `feat/verified-fetch-modes-calibration` | ANCESTOR |
| `work/12-merge-dispatch` | ANCESTOR |
| `main` | ANCESTOR |

The three commits the brief said this branch should carry are present and are
HEAD's own history: `d3dcd2b` / `5d7b96a` (gate wired into dispatch, floor under
the map), `888bd2f` + `5b3ae1a` (completeness floor over `control_command` and
`_agent_dispatch`), `2cec327` (`harness_tools` root confinement). Nothing in the
brief's lineage claim was false this time.

## The filed claim is false, in both halves

**Half one — "hardcoded copy of the permission rules".** There is no copy. The
app *derives* the matrix already:

- `server.permission_mode_data()` (`server.py:8119`) publishes
  `"matrix": dict(permission_modes._MATRIX[active])` — the enforcing module's
  own table, for the active mode.
- `sonder_serve.py:1962` serves it at `GET /v1/permission-mode`.
- `app/lib/api.dart:632` parses it into `PermissionMode.matrix`.

Single source, correctly plumbed. The app holds no matrix of its own.

**Half two — "no override channel".** One exists, and the app already reaches
it. `permission_rule_set` is catalogued as `/permission_rule_set` (risk
`dangerous`) and is reachable through the app's slash surface. Measured with
`permission_modes.decide(tool, mode=m, interactive=False)` — the HTTP surface's
own call shape:

```
permission_rule_set  risk=dangerous
    plan         -> deny
    manual       -> allow
    acceptEdits  -> allow
    auto         -> allow
```

Same for `runtime_policy_update` and `elevate`. This is the documented `ASK_CAVEAT`
degrade — `ask` with nobody to prompt becomes `allow` — not a new hole. But it
means the app can already rewrite the permission policy without a prompt in
three of four modes.

## What the app's matrix actually is: neither display-only nor decision-bearing

The load-bearing question was whether the app's copy is consulted for a
decision. It is consulted for nothing at all.

`grep -rn "\.matrix" app/lib` returns exactly one site — the field declaration
and its parse in `api.dart`. **Zero readers** outside `app/test`. The app fetches
the authoritative matrix every 30s, parses it, and drops it on the floor. It is
the fleet's recurring zero-caller shape, one layer up.

And the app displays *a different thing* instead: a per-command risk dot fed by
the `risk` field of `GET /v1/commands`.

## The constructed divergence (measured, not reasoned)

Two risk vocabularies were flowing over one wire field.

- `permission_modes._MATRIX` is keyed by **five** classes:
  `safe, ask, mutation, execution, dangerous`. `execution` is the synthetic
  class split out for tools that start a host process — it is the entire
  difference between `acceptEdits` and `auto`.
- `command_catalog._risk_for` — which produces the `risk` the app renders —
  could only ever return **four**: `safe, ask, mutation, dangerous`. It had no
  `execution` branch.

Measured over the live catalog (273 commands): **27 commands were published to
the app under a class the gate does not decide on.** All 27 gate to `execution`.
The four worst were published `safe`:

```
/build_run          published=safe  gate=execution
/dependency_audit   published=safe  gate=execution
/test_run           published=safe  gate=execution
/typecheck_run      published=safe  gate=execution
```

The app draws risk `safe` as a **green dot** with the tooltip **"Safe — read
only"** (`_riskColor` / `_riskLabel`, `app/lib/chat_screen.dart`). The runtime's
own answer for those four, from `_MATRIX[mode]["execution"]`:

```
plan -> deny    manual -> ask    acceptEdits -> ask    auto -> allow
```

So the app told the operator "Safe — read only", in green, for four tools that
launch a compiler or a test runner and that `plan` mode **refuses outright** —
on the same screen as the mode chip saying `plan`. The remaining 23 were
published `ask` or `mutation`; under `acceptEdits` all 27 resolve differently
via the published class than via the gate's class.

The same defect had a second surface: `command_catalog._RISK_MARK` is
`.get(risk, " ")` and had no `execution` entry, and `safe`'s mark is a blank —
so on the console `/help` listing, `/build_run` printed with the identical
blank mark as `/status`. A fail-open default under a missing key.

**Severity: Important, not Critical.** The app is not an enforcement point and
never decides — the gate's answer is unaffected. This is a confident wrong
answer given to a human, and the human is exactly who `plan` mode exists for.

## The fix — derives, does not sync

Fixed at the source so there is nothing to keep in step.

1. `command_catalog._risk_for` now emits `execution`, with the precedence
   `dangerous > execution > mutation > safe > ask` that mirrors
   `permission_modes.risk_of` exactly (so `self_heal_repair`, which is in both
   buckets, still reports `dangerous`). It reads
   `permission_modes.EXECUTION_TOOLS` / `EXECUTION_COMMANDS` **directly** rather
   than calling `risk_of` — `risk_of` resolves through `by_name`, which builds
   this catalog, so calling it from inside `catalog()` would recurse through a
   half-populated `lru_cache`.
2. New `_native_risk` covers native commands that front no registered tool and
   so never reached `_risk_for` at all. `/runwindow` was the live case: it took
   the legacy registry's `ask` while `EXECUTION_COMMANDS` classes it `execution`.
3. `_RISK_MARK` and the `/help` legend gained `>` for `execution`.
4. Deleted `command_catalog._RISK_ORDER` — a stale four-class tuple with
   **zero readers** anywhere in the tree. The live ranking is
   `sonder_repl._RISK_ORDER`, which has all five. A dead second copy of a policy
   vocabulary is worse than none, because the next reader takes it for the
   vocabulary.
5. App: `_riskColor` / `_riskLabel` gained an `execution` band, and the `ask`
   band stopped reading **"Asks before acting"** — that was a promise the row
   cannot keep, false under `acceptEdits` and `auto`. It now reads "Acts beyond
   a read — prompts depend on the mode".
6. `app/lib/api.dart` doc comment on `SonderCommand.risk` corrected from the
   four-class list to the five, with a note that it is now a valid key of
   `PermissionMode.matrix`.

This is derivation, not a sync step: the app receives one class from one
function and looks up nothing of its own; there is no copy to run out of date.

## A pre-existing test that encoded the defect as the requirement

`tests/test_serve_commands.py:53`:

```python
assert entry["risk"] in ("safe", "ask", "mutation", "dangerous")
```

Read before touching, per the standing rule. It is a contract-shape assertion,
not a deliberate security property — but it pinned the wire to the one
vocabulary the gate does *not* decide on, passed happily while all 27 commands
were mislabelled, and would have **rejected this fix**. Replaced with a
derivation from the enforcing module:
`assert entry["risk"] in permission_modes._MATRIX[permission_modes.MANUAL]`.
This makes eight such tests found in this repo.

## Guards added, and proof they bind

New file `tests/test_app_permission_surface.py` (3 tests):

- `test_published_command_risk_is_the_class_the_gate_decides_on` — every
  catalogued command's `risk` must equal `permission_modes.risk_of(name)`.
- `test_every_published_risk_class_is_a_key_of_the_matrix`.
- `test_flutter_app_draws_every_risk_class_the_matrix_defines` — parses
  `app/lib/chat_screen.dart`, brace-matches the bodies of `_riskColor` and
  `_riskLabel` (so a neighbouring switch's cases cannot be miscounted as this
  function's), and requires a `case` for every key of `_MATRIX`. There is
  precedent for Python tests reading Dart source here:
  `tests/test_deployment_safety.py`.

Three mutations, each planted, observed, reverted:

| mutation | result |
| --- | --- |
| remove `case 'execution'` from Dart `_riskColor` | `1 failed, 2 passed` — named `execution` |
| remove the `execution` branch from `_risk_for` | `1 failed, 20 passed` — listed all 27 commands by name |
| add a 6th class `network` to `_MATRIX[AUTO]` | `1 failed, 2 passed` — named `network` |

The third matters most: it proves the app guard is a floor over `_MATRIX`, not a
frozen list. Adding a risk class to the enforcing module now breaks the build
until the app is taught to draw it. `git diff permission_modes.py` is empty
after revert.

## Verbatim test summary lines

RED (3 items, at the final item count, before any fix):

```
FAILED tests/test_app_permission_surface.py::test_published_command_risk_is_the_class_the_gate_decides_on
FAILED tests/test_app_permission_surface.py::test_flutter_app_draws_every_risk_class_the_matrix_defines
2 failed, 1 passed in 1.28s
```

GREEN (new file):

```
3 passed in 1.66s
```

GREEN (every suite touched, 14 files):

```
649 passed in 39.74s
```

Suites run: `test_app_permission_surface`, `test_serve_commands`,
`test_permission_modes`, `test_command_catalog`, `test_command_router_catalog`,
`test_repl_catalog`, `test_permission_policy_display`,
`test_permission_gate_coverage`, `test_permission_gate_dispatch`,
`test_permission_gate_http`, `test_deployment_safety`, `test_command_registry`,
`test_server_helpers`, `test_tool_capabilities`. Also run separately and green:
`test_agent_dispatch_dev_tools`, `test_package_local_system`, `test_selfmod`,
`test_selfmod_commands`, `test_serve_history`, `test_isolated_run_server`,
`test_runtime_policy_server`, `test_unsafe_lab` (228 + 289 passed). The full
suite was **not** run, per the brief.

## The app's own test runner — NOT run

No Flutter or Dart SDK exists on this machine. `which flutter`, `which dart`
both empty; probed `C:\src\flutter`, `C:\flutter`, `C:\tools\flutter`,
`%LOCALAPPDATA%`, `%USERPROFILE%\flutter`, `%USERPROFILE%\fvm\default` — none
present. **`flutter test` was not run and I claim no Dart coverage.** What I can
state: `grep -rn "risk" app/test/*.dart` finds only fabricated-JSON parse
assertions on `SonderCommand.risk` (`'safe'`, `'mutation'`, `''`) and none on
the tooltip strings or colours I changed, so no existing Dart assertion targets
the edited code. That is inspection, not execution. The Dart edits are two added
`case` arms and one reworded string literal; the Python guard confirms both
switch bodies still parse with balanced braces, which is the only structural
check available here.

## Ruling: the app should NOT get a dedicated permission-override UI

The filed defect asks for an override channel. My ruling is that it should not
be built, and that the absence of a *dedicated* one is a deliberate security
property rather than a gap.

1. **It already exists.** `permission_rule_set` is the auditable, persistent,
   narrow override the runtime documents, and the app can already reach it.
   Building a second, friendlier one adds surface without adding capability.
2. **The default deployment does not authenticate.** `sonder_serve` ships
   `AUTH_MODE=local-open`, and the repo's own comment on `_dangerous_http_slash`
   says the answer to "who is calling" there is "anyone who can reach this
   port". A rule-editing UI on that surface is a privilege-escalation control
   behind no privilege check.
3. **`interactive=False` removes the only brake.** Measured above: on the HTTP
   surface `permission_rule_set` resolves to `allow` in manual, acceptEdits and
   auto. The `dangerous`-always-asks promise does not hold where the app calls
   from, so a UI there would edit policy with no prompt at all.
4. **The app already has the correct bounded control.** The mode picker offers
   the four modes the server publishes and nothing else — it can select among
   pre-defined policies but cannot invent a rule. That is the right shape for a
   remote client: bounded, server-defined, and unable to widen its own reach.

Recommend closing the "override channel" half of #28 as **won't fix, by design**.

## New findings

**Important (fixed here).** Two risk vocabularies over one wire field: the app
was told `safe` for four host-process launchers and drew them green as
"read only" while `plan` denied them. 27 of 273 commands affected.

**Important (open, NOT fixed).** `PermissionMode.matrix` has **zero readers** in
`app/lib` — fetched every 30s, parsed, discarded. Worth stating precisely: it
was not merely unused, it was **unusable**, because before this fix
`matrix[command.risk]` returned the wrong action for those 27 commands
(`matrix['safe']` = `allow` under plan, where the gate denies). That join is now
correct for every command, so wiring the dot's tooltip to
`matrix[command.risk]` — showing the resolved allow/ask/deny for the mode on the
chip, instead of a static band name — is now a genuine one-lookup change.
Deliberately left out of scope: it needs `PermissionMode` plumbed down to
`_CommandRow`, which is a UI change I cannot test without a Dart runner.

**Minor (fixed here).** `command_catalog._RISK_ORDER` had zero readers, making
it the third zero-caller found in this area.
