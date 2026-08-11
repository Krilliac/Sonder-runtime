# Tasks #39 / #40 — fixing the Critical and the Important from `merge-resolution.md`

Branch `work/12-merge-dispatch`, from merge commit `888bd2f`. Companion to
`merge-resolution.md`, which established the analysis this builds on. Every
`file:line` below was re-resolved against the tree rather than carried over —
the report's anchors had drifted (it cited `server.py:13537` for
`REPOSITORY_READ_ONLY_TOOLS`, which is at **13520**; `server.py:15892` for the
`repository_extra_roots=project` forward, which is at **15891**).

---

## 1. Task #39 (CRITICAL) — reproduction

Re-run first, unchanged, against a scratch canary directory outside the
repository. The "secret" is AWS's published documentation example key.

### Before

```
=== read_only=True, NO project root (repository_extra_roots='') ===
  secret_scan      ALLOWED  secret scan: 1 finding(s) in 3 files scanned
                            canary.py:1  [AWS credential]  AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EX...
  test_discover    ALLOWED  test discovery: pytest
  find_references  ALLOWED  references to 'Canary'
  diff_files       ALLOWED  diff --git "a/C:\Users\natew\AppData\Local\Temp\claude\...\canary\other.py" "b/...\other2.py"
```

Through `_agent_dispatch_observed` — the production wrapper autopilot's
`observe` policy reaches — with `project=""`. Confirmed
`_agent_project_scope('') -> ('', '')`, no error.

### After

```
=== read_only=True, NO project root (repository_extra_roots='') ===
  secret_scan      REFUSED  ERROR: read-only agent run has no host-selected project root, so developer-workflow tool 'secret_scan' has no project to work on. Pass project=<directory>.
  test_discover    REFUSED  ...
  find_references  REFUSED  ...
  diff_files       REFUSED  ...

=== CONTROL: read_only=True, host-selected project = the canary dir ===
  secret_scan      ALLOWED  secret scan: 1 finding(s) in 3 files scanned
  test_discover    ALLOWED  test discovery: pytest
  find_references  ALLOWED  references to 'Canary'
  diff_files       ALLOWED  diff --git a/other.py b/other2.py
```

The control is the point: a guard that refuses everything is not a guard. The
host-selected root still works, and Sonder's own workspace is still authorized
for the direct MCP callers.

---

## 2. Both layers, and why the second one is narrower than proposed

### Layer A — confinement in `harness_tools._resolve_root` (the real fix)

`_resolve_root` resolved any absolute path and returned it. It now refuses a
root outside `file_ops.allowed_roots(extra_roots)` — the same authorized set
(`workspace`, Sonder home, `SONDER_FILE_ROOTS`, `file_roots.local`) every
guarded file tool already honors, so it grants nothing new and takes nothing
the operator granted. `unsafe_lab.active()` and `file_ops.bypass_enabled()`
short-circuit it, mirroring `_file_bypass_allowed`.

All 23 entry points gained an `extra_roots=""` parameter forwarding to it. The
host-selected root reaches them through `harness_tools.authorized_root_scope()`,
a `contextvars` scope opened only by `_agent_dispatch` — **not** through a tool
argument. An `extra_roots` argument on the agent surface would be a root the
model grants itself; this is the same reasoning `_TRUSTED_REPOSITORY_APPROVAL`
encodes, and it is why `file_ops.resolve_path` honors `extra_roots` only under
bypass. Outside the scope these tools are confined to the operator's configured
roots, so a missed call site fails closed.

This is the layer that closes the class: it covers the direct `@mcp.tool()`
callers, which were never confined either, and it does not depend on any
dispatch-level set staying correct.

### Layer B — refusal at the door (defence in depth), **narrowed with evidence**

The report proposed refusing all of `_PROJECT_SCOPED_PATH_TOOLS` when
`read_only=True` and `repository_extra_roots` is empty. **Implemented as
specified, that is over-broad, and it is measured, not argued:**

```
FAILED tests/test_artifact_grounding_server.py::test_agent_and_loop_dispatch_artifact_ground
E  - artifact grounding: PASS
E  + ERROR: read-only agent run has no host-selected project root, so project-scoped tool 'artifact_ground' has no scope to inspect.
```

Reading `_repository_read_only_error`'s resolver chain (`server.py:13985`
onward) shows why. Every other project-scoped tool reachable on a read-only run
resolves its path through `file_ops.resolve_repository_read_path(...,
extra_roots=trusted_extra_roots)`; with no project bound, `trusted_extra_roots`
is `""` and they are confined to `allowed_roots("")`. They were never
unconfined. Of the 57 members of `REPOSITORY_READ_ONLY_TOOLS`, **exactly the
four developer-workflow tools have no branch in that chain** — which is the
Critical, stated precisely.

So Layer B keys on a new `_DEVELOPER_WORKFLOW_TOOLS` frozenset (the 23). It
covers all 23 rather than only the four in `REPOSITORY_READ_ONLY_TOOLS`, so
adding a tool to that set later cannot silently re-open the door.

I did not drop a layer. I narrowed one to the class that actually lacked
independent confinement, because refusing the rest broke a real capability and
closed nothing.

### The two layers are independently effective (mutation)

Layer B disabled (`if False:`), Layer A alone, through the production wrapper:

```
4 failed, 4 passed, 31 deselected in 1.42s
```

The 4 passed are `test_read_only_dispatch_is_refused_through_the_production_wrapper`
— **Layer A alone still closes the reproduction**. The 4 failed are the
assertions that pin *which* layer answered, which is what stops two locks that
can only be tested together from being one lock.

Layer A disabled (`_require_authorized_root` returning early):

```
25 failed, 14 passed in 6.81s
```

### Minor rider — `diff_files` absolute host path leak: **fixed**

`git diff --no-index` echoes its arguments into the `diff --git` header, so
absolute arguments printed the operator's full host path — and the account name
in it — into every diff a *confined* agent read back. It now passes paths
relative to `root`:

```
before: diff --git "a/C:\Users\natew\AppData\Local\Temp\claude\...\canary\other.py" "b/..."
after:  diff --git a/other.py b/other2.py
```

Resolving relative to the root also closed a second hole at the same call site:
`left` and `right` were joined to `root` and never checked, so `../..` walked
straight out of it for any caller not going through the agent surface's own
scope check. Now refused (`test_diff_files_refuses_a_path_escaping_its_root`).

---

## 3. Task #40 (IMPORTANT) — the floor now watches `_agent_dispatch`

`_CHAINS` gained a fourth entry, `("server.py", "_agent_dispatch", "agent")`.
Two mechanical differences from the three slash chains, both handled:

* The agent chain compares `tool_name` against bare names, not `cmd` against
  `/slash` names, so it gets `_tool_branch_names` — written out in the test file
  rather than imported from `tool_capabilities.dispatch_names`, keeping this
  file's stated discipline that it is the check *on* those derivations.
* The map is `_agent_tools()`, built from `command_catalog.catalog()` **and the
  alias table** — never from the dispatch list it checks, which would agree by
  construction and could never report a hole.

### RED — Mutation B, reused verbatim

```python
if tool_name == "mutation_probe_tool":
    return "probe"
```

```
E       AssertionError: 1 dispatch branch(es) resolve to no tool, so the gate is consulted, receives an empty set, and allows them:
E           mutation_probe_tool (server.py, agent)
FAILED tests/test_permission_gate_coverage.py::test_every_dispatch_branch_is_in_the_map_or_declared_display_only
1 failed, 7 passed in 3.45s
```

Against `8 passed` for the identical mutation before this change. `server.py`
restored from a byte-exact copy, verified `sha256sum -c` → `server.py: OK`.

### GREEN, and what the extended floor named

```
8 passed in 3.50s
```

Measured coverage:

```
server.py        control_command    console  branches=96
sonder_repl.py   main               console  branches=152
sonder_serve.py  _handle_slash      http     branches=130
server.py        _agent_dispatch    agent    branches=140
maps: {'console': 134, 'http': 128, 'agent': 281}
holes: 0 []
```

**It named zero holes, and zero is the classic tell, so here is why this one is
real.** The raw dispatch names contain 7 the catalog has never heard of —
`agent_cancel`, `agent_capacity`, `agent_retry`, `agent_status`,
`game_campaign`, `game_generate`, `improvement_report`. All 7 are
`_AGENT_TOOL_ALIASES` keys, and `_agent_permission_gate_error` (`server.py:14504`)
canonicalizes with `_canonical_agent_tool_name` **before** grading. Reading raw
names would have reported 9 holes the gate in fact handles correctly. After the
same canonicalization the gate performs: 0 uncatalogued. The 0 is corroborated
three ways — the vacuity control asserts >50 branches per chain and >100 map
entries per map (it passes at 140/281), Mutation B turns it into 1, and the
count was measured by enumeration, not inferred from a subtraction.

**One thing the extended floor deliberately does not flag.** 28 dispatchable
tools grade `risk_of == "ask"`, and `decide(interactive=False)` degrades `ask`
to *allow*, so on this path they are permitted in every mode. That is not the
fail-open fallback — it is the catalog's explicit grade, and allowing it with
nobody at a keyboard is Lane 1's written design decision
(`_agent_permission_gate_error`'s docstring). Flagging it here would be this
file re-litigating a policy instead of checking a map. Recorded because it is
the kind of thing a later reader will otherwise rediscover as a finding.

---

## 4. NEW finding — two tests encoded the hole as a requirement

`tests/test_agent_dispatch_dev_tools.py::test_read_only_dispatch_reaches_test_discover`
asserted, verbatim:

```python
out = server._agent_dispatch("test_discover", {"root": "."}, read_only=True)
assert out == "discovered:."
```

That is the vulnerability written down as intended behaviour: a read-only
dispatch reaching a developer-workflow tool with no project bound at all. It
was the only test in either lane's scope that Layer B broke. Rewritten to
assert the same real intent — read-only dispatch must reach the read-only
tools — with a root bound, plus the negative half.

`test_mutating_dev_workflow_tools_are_refused_by_read_only_dispatch` needed the
same treatment for a subtler reason: it kept passing, but Layer B refuses before
`_repository_read_only_error` is ever consulted, so it would have gone on
proving only that, never reaching the read-only policy it exists to check. Same
refusal, different reason. It now binds a project root and asserts the refusal
is *not* the rootless one.

---

## 5. Tests run

Full suite (~522s) deliberately not run. Files run, verbatim summary:

Both lane scopes from `merge-resolution.md` §5, plus everything this change
touches (`test_harness_root_confinement.py`, the four `test_harness_*` files,
`test_artifact_grounding_server.py`, `test_artifact_fetch.py`,
`test_file_ops.py`, `test_file_ops_containment_degradation.py`,
`test_package_local_system.py`, `tests/production/test_architecture.py`,
`test_release_artifacts.py`):

```
1038 passed, 7 skipped in 75.50s (0:01:15)
```

The four `test_harness_*` files gained an autouse fixture authorizing pytest's
tmp tree via `SONDER_FILE_ROOTS` — they legitimately work in `tmp_path`, the way
an operator authorizes a repository in `file_roots.local`. That fixture
*authorizes a root*; it does not disable the check, and
`tests/test_harness_root_confinement.py` is what makes the difference
observable. Without that file the fixtures would be indistinguishable from
deleting the guard.

---

## Provenance

Produced 2026-08-11 in worktree `D:\sonder-wt\12-merge-dispatch` on branch
`work/12-merge-dispatch`. No `git stash` was run and the stash refs were not
touched; no `git add -A` was run. `sdd/01-permission-gate` and
`sdd/02-calibration` were not modified. Nothing was pushed. The full test suite
and the live benchmark were not run. Mutations were applied to `server.py` and
`harness_tools.py` and reverted from byte-exact copies, verified with
`sha256sum -c` (`server.py: OK`) before committing. The canary directory and
every probe script live in the session scratchpad, never inside the repository
and never in the operator's real home; the canary value is AWS's published
documentation example key, not a credential.
