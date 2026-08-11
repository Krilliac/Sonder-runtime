# Tasks #21 / #31 — the self-modification gate, and a fall-through seen twice

Branch `work/17-selfmod-gate`, from `5b3ae1a`. Companion to
`merge-resolution.md` and `fix-critical.md`, whose gate wiring and root
confinement this builds on rather than revisits. Every `file:line` was
re-resolved against this tree.

---

## 1. #21 — CONFIRMED in substance, one half of the filed claim refuted

The claim had two halves. They did not both survive.

**Refuted: "not stopped by `plan` mode."** `7cb5052` graded `/selfmod`
`dangerous` in `command_registry.py:103`, and `command_catalog`'s
`_UNREGISTERED_BRANCH_WORK` (`command_catalog.py:332`) is what makes that grade
reach a command whose real work is done by module functions fronting no
registered tool. `plan` denies `dangerous` at every surface. Measured, not read.

**Confirmed: ungated in every other mode.** `dangerous` maps to `ask` under
`manual`, `acceptEdits` and `auto` (`permission_modes._MATRIX`), and every
surface reaching `_selfmod_command` decides with `interactive=False`, where
`ask` degrades to `allow`. The gate was consulted, correctly graded the command
as the most dangerous class it has, and let it through anyway.

Reproduction, driven through the real path with `selfmod.deploy`/`rollback`
replaced by probes — the live installation was never deployed to:

```
deploy   plan         control_command    REFUSED       refused /selfmod: plan forbids dangerous tools
deploy   manual       control_command    REACHED-TOOL  STUB: deploy reached
deploy   acceptEdits  control_command    REACHED-TOOL  STUB: deploy reached
deploy   auto         control_command    REACHED-TOOL  STUB: deploy reached
rollback manual       control_command    REACHED-TOOL  STUB: rollback reached
deploy   manual       http _handle_slash REACHED-TOOL  STUB: deploy reached
deploy   auto         http _handle_slash REACHED-TOOL  STUB: deploy reached
rollback auto         http _handle_slash REACHED-TOOL  STUB: rollback reached
```

12 of 16 mode/action/surface combinations reached the write path. After:

```
deploy   manual       control_command    REFUSED  refused /selfmod deploy: this rewrites Sonder's own source...
deploy   auto         http _handle_slash REFUSED  refused /selfmod deploy: this rewrites Sonder's own source...
```

All 16 refused; the 4 `plan` rows still refuse for the reason they already did.

**Agent dispatch and direct MCP cannot reach it at all**, and that is measured
rather than assumed: `selfmod` is catalogued as a *command*, and both
catalogued fall-throughs resolve a handler by name — `globals().get("selfmod")`
in `server.py` and `getattr(server, "selfmod")` in `sonder_serve.py` — which
returns the *module*. A module is not callable, so both decline it. The reach
is the three slash chains, all of which funnel through `control_command`.

### The per-mode decision, and why `ask`→`allow` must not apply here

| mode | `/selfmod deploy\|rollback`, nobody present | why |
| --- | --- | --- |
| `plan` | DENY (unchanged) | already held; holding still is the point |
| `manual` | DENY (was allow) | `ask` with nobody to ask must not mean yes |
| `acceptEdits` | DENY (was allow) | this is not an edit git can revert |
| `auto` | DENY (was allow) | `auto` never promised to bypass `dangerous` |
| any, console operator answered | ALLOW | a person actually said yes |
| any, explicit `allow` rule | ALLOW | narrow, written down, auditable |

**`ask`→`allow` degradation should not apply to self-modification.** The
degrade is deliberate and correct for ordinary tools, and I left it alone for
them. Its justification is a trade: accept an unanswerable prompt resolving to
yes, because the result can be undone afterwards. That trade assumes the thing
that would undo it still works. `selfmod.deploy` `os.replace`s Sonder's own
source tree — it is the one operation in this repository that can overwrite the
interpreter that would perform the recovery. Every other `dangerous` tool can
be undone by running Sonder; this one can overwrite the Sonder that does the
undoing. So the assumption the degrade rests on is precisely what is at stake,
and the trade stops paying.

The refusal is kept actionable, because this branch has already recorded that a
refusal nobody can act on trains operators to route around the gate. Two routes
out survive and both are tested: a console operator who answers the prompt, and
an explicit `allow` rule, which `decide()` resolves at step 3 before the
degrade is ever consulted.

### What was changed

* `permission_modes.decide(..., non_degrading=False)` — per-invocation, exactly
  like the existing `requires_elevation`, and for the same stated reason: what
  makes an operation unrecoverable is what the caller asks a general entry
  point to do, not the entry point. `ask` + nobody + `non_degrading` → `DENY`.
* `server._SELFMOD_SOURCE_WRITING_ACTIONS = {"deploy", "rollback"}` and a second
  gate in `_selfmod_command`. **Keyed on the action, not the command**:
  `/selfmod status` arrives at the same entry point, and refusing a status read
  unattended would be the over-refusal this gate exists to avoid.
* `control_command(..., operator_approved=False)`, forwarded to
  `_selfmod_command`; `sonder_repl` passes `_console_has_operator()`.
  Without it the re-decision inside `control_command` would have overruled the
  person who had just approved at `_named_command_gate`, turning `/selfmod
  deploy` off entirely rather than gating it.

`operator_approved` is a keyword rather than a `contextvars` scope because the
self-authorization risk that motivated `harness_tools.authorized_root_scope`
is absent here for a *checkable* reason, and the check is a test:
`control_command` is not a registered tool and the catalog has never heard of
it, so neither fall-through nor agent dispatch can resolve it and no model can
set the argument.

### One thing I did not do

`plan` refuses `/selfmod status`. My first draft of the test file asserted it
should not. That is wrong: the chain gate grades a named command by the
strictest tool it can front (`sonder_repl._gate_tools`), which is deliberate
and settled, and asserting otherwise would have re-litigated the grading rule
under cover of testing this fix. The test is scoped to the unattended modes
this change is responsible for, with the reasoning written into it.

---

## 2. #31 — REFUTED. The same fall-through, seen twice.

`d3dcd2b` ("The chain was gated and the fall-through under it was not") fixed
**both** fall-throughs, not just `control_command`'s. `git log -L` on the app's
path is unambiguous — the refusal at `sonder_serve.py:1321` was added by that
same commit:

```
d3dcd2b The chain was gated and the fall-through under it was not
+    refusal = _http_tool_refusal((tool_name,), "/" + tool_name)
+    if refusal:
+        return refusal
```

Verified by execution rather than by reading the diff, at
`_dispatch_catalogued_tool` — the path reached by the *tool's own name*, which
no named command covers:

```
/file_delete    plan   REFUSED       refused /file_delete: plan forbids dangerous tools
/sqlite_mutate  plan   REFUSED       refused /sqlite_mutate: plan forbids dangerous tools
/file_delete    auto   REFUSED       refused /file_delete: rule denies this tool     (deny rule)
/sqlite_mutate  auto   REFUSED       refused /sqlite_mutate: rule denies this tool   (deny rule)
/file_delete    manual REACHED-TOOL  DELETED
```

The `manual`/`auto` rows are the deliberate `ask`→`allow` degrade for an
ordinary `dangerous` tool with nobody to ask — the documented semantics, not a
bypass. The gate is present and binding.

Recorded because it nearly became a false finding: my first `/sqlite_mutate`
probe passed `db=` instead of `path=`, so `parse_invocation` rejected it before
the gate and it printed REFUSED in all three modes. That is a probe error
wearing the costume of a clean result. Re-run with the right parameter, it
degrades in `manual`/`auto` exactly like `file_delete`. A refusal that comes
from the wrong layer is not evidence about the layer under test.

**I did not extend `_CHAINS` and did not patch an entry point for #31, because
there is nothing to fix.** For the record on the question as posed: the app's
fall-through is not a chain the floor should cover. The floor checks that every
dispatch *branch* resolves to a tool in the map; the fall-through has no
branches — it resolves the tool by its own name and gates that name directly,
so it is covered by construction and adding it would be a check that agrees
with itself.

---

## 3. Every guard binds — mutation

Backups byte-exact, each revert verified with `sha256sum -c` (all three `OK`).

| # | mutation | result |
| --- | --- | --- |
| A | `if non_degrading:` → `if False:` in `decide` | `14 failed, 14 passed` |
| B | drop `"deploy"` from `_SELFMOD_SOURCE_WRITING_ACTIONS` | `8 failed, 20 passed` |
| C | `operator_approved=_console_has_operator()` → `=True` in `sonder_repl` | **`28 passed` — SURVIVED** |
| D | delete the `operator_approved=` wiring from `sonder_repl` | (see below) |

**Mutation C survived, and that mattered.** All 28 tests were green with the
console hardcoded to claim an operator, because every test built its own call
to `control_command` and none read what the repl actually passes. The refusal
was proven; the thing deciding whether the refusal applies was not — a guard
with nobody holding it. A literal there hands `sonder < script.txt`, a console
session with nobody at the keyboard, the approval a person never gave.

Closed with `test_the_console_never_hardcodes_the_operator_approval`, an AST
check on `sonder_repl` (the discipline the coverage floor already uses), plus a
vacuity assertion that the argument is passed at all. Re-running the mutations
against it:

```
C (hardcoded True):     1 failed, 28 passed
D (wiring removed):     1 failed, 28 passed
```

---

## 4. Can deploy damage its own rollback path?

**Normally no; under one operator-typed flag, yes — and the health check does
not close that window.**

`selfmod.py` and `selfmod_recover.py` are both in `SENSITIVE_PREFIXES`
(`selfmod.py:38`), and `_protected()` is enforced at three independent points:
`create_plan` (`:399`), the candidate write loop (`:636`), and review
(`:818`). An ordinary run cannot touch the rollback path at all.

`--maintenance` (`server.py`, `/selfmod plan|run`) sets
`maintenance_authorized` and lifts all three. `max_files_changed` is 8, so a
single authorized run can have both `selfmod.py` (the in-process `rollback`)
and `selfmod_recover.py` (the standalone recovery tool) in scope.

The recovery story is **not** fiction: `selfmod_recover.py` "intentionally
imports no Sonder modules" and restores from a backup manifest plus
`manifest.sha256` held outside the source tree, so a copy from git or backup
restores a bricked tree. Deploy also runs a post-deploy health check and
auto-`restore()`s on failure, and the `except Exception` path restores too.

**NEW finding, Important, out of scope — the health check cannot see a broken
rollback.** The health command is `[python, "-c", "import server;
print(server.status())"]`. It proves the new bytes *import*; it never calls
`selfmod.rollback`. A `--maintenance` deploy that leaves `selfmod.py`
importable but `rollback` semantically broken passes health, is marked
`deployed`, and has no working rollback — the one state the auto-restore exists
to prevent. Narrow (requires `--maintenance`), but the mitigation is stated in
the code as though it were general. Not fixed here: changing what the health
command proves is a change to deploy's contract, not to the gate.

---

## 5. Tests run

Full suite (~522s) deliberately not run. Files run, verbatim summaries.

New file, RED at the final item count (20 items), failing behaviourally with
`_Reached`, an `AssertionError` subclass — *"selfmod.deploy was reached with
nobody asked"*:

```
13 failed, 7 passed in 6.18s
```

GREEN, same file after the fix and the two added guards (29 items):

```
29 passed in 2.14s
```

Four of the 29 are labelled in the file as guards rather than reproductions:
they exercise the keyword the fix introduces, so at the parent commit they
would fail on the signature, not on behaviour. Saying so is the point.

Regression batch — the gate suites, both selfmod suites, the catalog, and
release hardening:

```
520 passed, 1 skipped in 93.30s (0:01:33)
```

(`test_selfmod_deploy_gate`, `test_permission_gate_dispatch`,
`test_permission_gate_http`, `test_permission_gate_coverage`,
`test_permission_modes`, `test_selfmod`, `test_selfmod_commands`,
`test_spec5_selfmod`, `test_repl_catalog`, `test_command_catalog`,
`test_agent_verification_gate`, `production/test_release_hardening`.)

---

## Provenance

Produced 2026-08-11 in worktree `D:\sonder-wt\17-selfmod-gate` on branch
`work/17-selfmod-gate`. **`/selfmod deploy` was never run against the live
installation**: `selfmod.deploy` and `selfmod.rollback` were replaced by probes
that record and raise, in both the pytest fixtures and the standalone repro,
and every probe ran against a hermetic `SONDER_HOME` in the session scratchpad.
No `git stash` was run and the stash refs were not touched. No `git add -A` was
run. No sibling worktree was modified. Nothing was pushed. The operator's
memory DB and stored facts were not touched, the live benchmark was not run,
and no vendored `app/build/**/local-system/*.py` copy was edited. Mutations
were applied to `server.py`, `permission_modes.py` and `sonder_repl.py` and
reverted from byte-exact copies, verified with `sha256sum -c` before
committing.
