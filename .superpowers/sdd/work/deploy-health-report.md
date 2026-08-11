# Task #46 — the post-deploy health check that never asked whether it could undo itself

Branch `work/22-deploy-health`, from `39d78ca`. Companion to
`selfmod-gate-report.md`, which established the `/selfmod` non-degradable gate
and found this defect as an out-of-scope rider. Nothing here deploys to the
live installation: every run below uses a scratch repository under a temp dir
and an isolated `SONDER_SELFMOD_HOME`/`SONDER_SELFMOD_DB`.

---

## 0. Lineage, verified rather than assumed

```
git merge-base --is-ancestor feat/verified-fetch-modes-calibration HEAD  -> ANCESTOR
git merge-base --is-ancestor work/12-merge-dispatch                 HEAD  -> ANCESTOR
git merge-base --is-ancestor 9f377f1                                HEAD  -> ANCESTOR
```

And the four guarantees this branch is supposed to carry are present in the
tree, not merely in the history:

| guarantee | evidence in this checkout |
| --- | --- |
| `/selfmod` non-degradable gate | `permission_modes.non_degrading` (3 sites), `server._SELFMOD_SOURCE_WRITING_ACTIONS` (3 sites) |
| permission gate | `tests/test_permission_gate_dispatch.py` + `_http.py`, 142 passed |
| completeness floor over `_agent_dispatch` | `server.py:13679` `tool_capabilities.dispatch_names(_agent_dispatch)` |
| `harness_tools` root confinement | `harness_tools._resolve_root`/`authorized_root_scope`, 25 sites |

---

## 1. #46 — CONFIRMED, and worse than filed on one axis

`server.py:2125` deployed with

```python
health_command=[sys.executable, "-c", "import server; print(server.status())"]
```

That proves the newly written bytes *import*. It never calls
`selfmod.rollback`, and it never touches `selfmod_recover`.

`selfmod.py` and `selfmod_recover.py` are `_protected()` (`selfmod.py:38-48`)
and enforced at three points — `create_plan` (`:399`), `apply_candidate_changes`
(`:636`), `review` (`:818`) — so an ordinary deploy cannot rewrite them. Under
an operator-typed `--maintenance` run all three enforcement points defer, and
both files fit inside one eight-file deploy.

### Reproduction

A `--maintenance` run whose candidate meets its declared objective, passes
every pre-deploy check, imports perfectly, and whose rollback is broken:

```
run risk=critical maintenance_authorized=True files=['selfmod.py']
  pre-deploy check syntax     passed=True exit=0
  pre-deploy check targeted   passed=True exit=0
  pre-deploy check regression passed=True exit=0
  pre-deploy check smoke      passed=True exit=0
  pre-deploy check security   passed=True exit=0
after review: phase=reviewing last_error=''
after approve: phase=approved

HEALTH COMMAND (verbatim from server.py:2125): [python, '-c', 'import server; print(server.status())']
after deploy: phase=deployed
post_deploy_health exit=0 passed=True output="{'ok': True, 'selfmod_note': 'v2'}"

DEPLOYED rollback() invoked: exit=1
NameError: name '_restore_phase' is not defined
live selfmod.py is the broken v2: yes

VERDICT: phase=deployed with a rollback that raises when called.
```

Marked `deployed`. Health green. No working rollback.

### The axis the filing did not mention

`scripts/nightly_selfmod.py:684` calls

```python
selfmod.deploy(run_id)
```

with **no `health_command` at all**. The unattended `auto-low-risk` lane —
the one lane with nobody present to notice — had *zero* post-deploy
verification. Any fix scoped to "strengthen the argv at `server.py:2125`"
would have left that lane exactly as it was. This is why the fix went into
`deploy()` rather than into its caller.

The recovery *architecture* was not at fault and is not revisited.
`selfmod_recover.py` imports no Sonder modules and restores from a
checksum-gated out-of-tree manifest. That design is sound. What was missing
was a check that the deployed bytes can still run it.

---

## 2. What the check now does, and why that shape

`selfmod.deploy` now runs a rollback-readiness probe **first, and
unconditionally**, immediately after the phase moves to `deployed` and before
any caller-supplied `health_command`. The property is enforced at the layer
that owns it, so it cannot be lost by editing an argv or by a caller that
passes none.

`selfmod.verify_rollback_ready(run_id)` — executed by the *deployed* code, in a
child process, with `.` first on `sys.path`, so it is the just-written bytes
that answer:

1. `verify_backup` — the backup bundle still hashes as recorded.
2. `_deployed_file_mismatches` — the state a real `rollback()` checks first.
   If it would refuse, say so now rather than at 3am.
3. **In-tree route.** `_restore_manifest_files` — what
   `rollback()` -> `restore()` funnels through — redirected at a temporary
   directory, then the restored files re-hashed *from disk*.
4. **Out-of-tree route.** A manifest bundle is written pointing at a second
   temporary directory and `selfmod_recover.restore` is called on it, so its
   checksum gate, backup verification and path confinement all execute. Its
   returned root is checked to be the scratch tree.
5. Both digests must agree, and the receipt is printed.

Against the three constraints:

* **Cannot corrupt state if it fails.** It writes only inside a
  `TemporaryDirectory`, and it runs in a child process. When it fails, the
  restore is performed by the already-loaded module — which is by construction
  the code from *before* this deployment. That is the whole reason the check is
  worth having at this point in the sequence.
* **No network, no model, no operator.** stdlib subprocess, local files, 719 ms
  measured for a one-file run.
* **Fails loudly and specifically.** The refusal names what it caught
  (`NameError: name '_manifest_root_for_restore' is not defined`), the failed
  probe is persisted as a `post_deploy_rollback` row with its full output, and
  the run is auto-restored.

### Why a receipt and not an exit code

"It returned 0" for "it did the thing" is the same defect shape one layer up.
So `expected_rollback_receipt` is computed **by the deploying process** from
the manifest, and is deliberately *never passed to the probe*. The child must
reproduce a SHA-256 over the paths and the bytes it actually restored. A
deployed `verify_rollback_ready` reduced to `return "ok"` exits 0 and is
rejected; one that prints a fabricated receipt is rejected too.

Scope stated honestly in the docstring: this catches a rollback that is
*broken*. It is not a defence against a tree that deliberately forges its own
receipt — nothing running inside the tree can settle that.

---

## 3. Proof that it binds — plant and revert

Six `--maintenance` deploys, scratch repositories whose `selfmod.py` and
`selfmod_recover.py` are byte copies of this checkout's:

```
A. baseline -- sound maintenance deploy (must PASS)
  deploy: SUCCEEDED, phase=deployed
  post_deploy_rollback: passed=True exit=0 output=SELFMOD-ROLLBACK-RECEIPT intree=7c6cf4fa... recover=7c6cf4fa... files=1

B. planted: _restore_manifest_files raises when called (must REFUSE)
  deploy: REFUSED -- deployed code cannot perform a rollback; automatic rollback completed: NameError: name '_manifest_root_for_restore' is not defined
  phase after refusal: restored
  live selfmod.py carries the candidate marker: False

C. planted: verify_rollback_ready stubbed to a no-op (must REFUSE)
  deploy: REFUSED -- ... the probe exited 0 but did not report a matching rollback receipt; expected 'SELFMOD-ROLLBACK-RECEIPT intree=7c6cf4fa... files=1'
  post_deploy_rollback: passed=False exit=0 output=

D. planted: verify_rollback_ready prints a forged receipt (must REFUSE)
  deploy: REFUSED -- ... the probe exited 0 but did not report a matching rollback receipt
  post_deploy_rollback: passed=False exit=0 output=SELFMOD-ROLLBACK-RECEIPT intree=000000... files=1

E. planted: selfmod_recover.restore raises when called (must REFUSE)
  deploy: REFUSED -- ... NameError: name '_emergency_root' is not defined

F. reverted -- sound again (must PASS)
  deploy: SUCCEEDED, phase=deployed
  post_deploy_rollback: passed=True exit=0 output=SELFMOD-ROLLBACK-RECEIPT intree=7c6cf4fa... recover=7c6cf4fa... files=1
```

The tracebacks in B and E name the scratch tree's own `selfmod.py`, which is
the evidence that the probe ran the *deployed* bytes and not the installed
ones. C and D are the anti-proxy cases: exit 0, refused anyway.

---

## 4. One over-refusal found and fixed during implementation

The first cut pinned the child to `sys.path.insert(0, '.')` only. That made
`deploy()` refuse against any repository that is not itself a Sonder
installation — four pre-existing tests in `tests/test_selfmod.py` (which deploy
to a `calc.py` scratch repo) failed with
`ModuleNotFoundError: No module named 'selfmod'` reported as
"deployed code cannot perform a rollback".

I read those tests before touching anything; they were correct and I did not
weaken them. A `ModuleNotFoundError` dressed up as a broken rollback is a
failure for the wrong reason, and a check that fails for the wrong reason
trains people to route around it. `rollback_probe_command` now puts `.` first
and appends the deploying process's own `sys.path`, so a Sonder tree gets the
deployed bytes and any other tree still gets a real dry-run rollback from the
running module.

---

## 5. TDD record

RED at the parent (`39d78ca`, before any change to `selfmod.py`):

```
5 failed in 16.87s
```

Each failed behaviourally, not by import or collection error:

```
E       Failed: DID NOT RAISE <class 'RuntimeError'>          x4
E       AssertionError: expected exactly one recorded rollback verification, got 0
```

The four `DID NOT RAISE` rows *are* the defect: at the parent, a maintenance
deploy of a candidate whose rollback is broken completed and was marked
`deployed`.

GREEN, and the pre-existing selfmod surface with it:

```
89 passed in 77.96s (0:01:17)
```
`tests/test_selfmod_deploy_health.py` (new, 5), `tests/test_selfmod.py`,
`tests/test_selfmod_commands.py`, `tests/test_selfmod_deploy_gate.py`,
`tests/test_spec5_selfmod.py`. Plus `tests/test_permission_gate_dispatch.py`
and `tests/test_permission_gate_http.py`: `142 passed in 8.04s`.
The full suite (~522s) was not run.

---

## 6. Sibling sweep — proxy verification elsewhere

Repo-wide sweep for post-action verification that checks a proxy rather than
the property claimed. `app/build/**` (vendored), `tests/**`, and pre-action
gates were excluded. Every finding below was re-read at the cited line before
being written down.

### FOUND — new, not filed, not fixed here

**#S1 (Critical) `server.py:1766` — the selfmod `smoke` gate cannot fail.**

```python
smoke = [sys.executable, "-c", "import pathlib; assert pathlib.Path('.').is_dir(); print('selfmod smoke ok')"]
```

`_selfmod_test_commands` builds the acceptance battery run after the editing
agent has written into the candidate workspace, and `selfmod.review()`
(`selfmod.py:797`) requires a **passing** check of kind `smoke` before a
candidate may be approved. The command asserts that `.` is a directory. It
never imports, runs or reads the candidate; `passed` is `exit_code == 0`, so it
is `True` unconditionally.

This is #46's own shape one stage earlier in the same pipeline, and it is worse:
the post-deploy check at least ran the candidate. This one is a required gate
that inspects nothing. I did not fix it — it changes what `review()` accepts,
which is a different blast radius from a post-deploy probe and deserves its own
reproduction.

**#S2 (Important) `server.py:1763` — the `syntax` gate degrades to a constant pass.**

```python
python_files = [path for path in run["files"] if path.endswith(".py") and (workspace / path).is_file()]
syntax = [...py_compile, *python_files] if python_files else [sys.executable, "-c", "print('no Python syntax targets')"]
```

When `python_files` is empty the required `syntax` check becomes a `print` that
always exits 0 and is recorded as passing. The `.is_file()` filter silently
empties it when a candidate **deletes** its declared `.py` files — so the run
most likely to need a syntax gate is the one that gets a no-op.

**#S3 (Important) `sonder_update_engine.py:345` — migrate trusts an exit code and
discards the payload it asked for.**

```python
migrate_result = self._run_in_release(final_dir, ["-m", "sonder_runtime", "migrate", "--json"], ...)
if migrate_result.returncode != 0: ... roll back ...
self.repository.record_step(update_id, step, "migrate", "ok")
```

`--json` is requested and never parsed; `pending` is never read.
`sonder_migrations.migrate_store:332` returns early when `discover_migrations`
finds nothing — *before* the `unknown`/checksum gates. A staged release whose
`migrations/` tree was omitted from the bundle is a silent no-op that exits 0,
is journalled `"ok"`, and activates. The adjacent `health_check` step at
`sonder_update_engine.py:377-394` already carries the fix for exactly this
("no check actually ran, so an empty problem list is not a pass"); the migrate
step did not get the same treatment.

**#S4 (Important) `assetgen.py:852` + `artifact_grounding.py:3587` — a
tautological required-kinds check.** Both sides read the same
`manifest["kinds"]`, so `kind in kinds` compares a list with itself and can
never fail. `manifest["kinds"]` is the *requested* kinds
(`assetgen.py:767`), never cross-checked against files on disk, so a kind whose
writer silently produces nothing passes. Hashes are verified, so corruption is
caught; absence is not.

**#S5 (Minor) `scripts/scaffold_verify.py:29,93` — "VERIFIED" for a
syntax-only toolchain.** Python scaffolds are checked with `compileall` over an
empty `__init__.py` and a `print()`; `pyproject.toml`, the part most likely to
be wrong, is never parsed. `rust`/`go`/`csharp`/`cpp-*`/`java-maven` run real
builds.

### Residual on the fixed site, corrected in this branch

My own first comment at `server.py:2126` claimed the health command proves "the
server still answers". It does not: `server.status()` catches `ModelCallError`
and `urllib.error.URLError` and *returns* the error as a string, so the command
exits 0 with `ERROR contacting Ollama...` in output nothing reads. The comment
now says what the command actually proves and tells the next reader not to grow
claims for it. The `post_deploy_health` row's `passed` is still `int(code == 0)`
with `output` stored and never inspected — left as is, because the rollback
probe that now runs first is the check that carries the recoverability
property.

### CHECKED AND CLEARED

Read in full: `safe_update.py`, `self_heal.py` (re-runs `check()` and returns
the fresh issue list), `store_integrity.py`, `live_reload.py`,
`sonder_migrations.py` (ledger/checksum authority is sound; the defect is at
the caller, #S3), `sonder_backup.py` / `sonder_runtime/adapters/backup.py`
(per-file sha256 plus `PRAGMA integrity_check`, `foreign_key_check` and
migration-ledger health on the restored copy — genuinely strong),
`sonder_doctor.py` (unknown verdicts default to `fail`), `sonder_health.py`,
`verifiers.py`, `node_verifier.py`, `ruff_verifier.py`, `sql_verifier.py`,
`json_schema_verifier.py` (generate→verify oracles, each scoped honestly;
`ruff_check` documents itself as "a *style* gate, not a correctness oracle"),
`import_autofix.py`, `refinement_transactions.py`, `engine_bundle.py`
(re-hashes every copied file after the copy, before `os.replace`),
`model_assets.py`, `project_scaffold.py`, `codegen_loop.py` (the whole module
is an anti-proxy argument — `build_ran`, `count_unreliable`,
`describe_total`), `self_curriculum.py`, `scripts/nightly_selfmod.py` (honours
`review()`'s verdict), `npu_manifest.py`, `artifact_grounding.py` structure
validators (enumerate files on disk rather than trusting manifest rows).

Grep sweep, inspected and cleared: `adaptive_training.py:1980-1994, 2652-2656`
(post-`ollama cp` verifies model identity hash + digest, not just exit 0),
`:922`, `:988`, `:1250-1266` (sealed Git blob SHAs via `hmac.compare_digest`),
`json_patch_tool.py:321` (re-reads and byte-compares after `os.replace` —
textbook), `archive_tools.py:461`, `artifact_fetch.py:781`,
`sonder_updates.py:195` and `:558`, `sonder_update_engine.py:377-394`,
`bootstrap_engine.py:201-209` (re-runs the import probe rather than trusting
pip's exit code), `scripts/assemble_engine_bundle.py:226-236`,
`scripts/npu_provision_embedder.py:87` (cosine-similarity equivalence against
the live embedder with a floor), `server.py:5360-5408` (separates attributable
pytest exit codes 0/1/2 from infrastructure 3/4/5/timeout),
`server.py:16312-16322`, `game_ladder.py:113`, `grounding.py:249, 371-384, 414`,
`isolated_runner.py:510` (`(st_dev, st_ino, S_IFMT)` TOCTOU guard).

Noted and judged adequate rather than reported: `setup_alias.py:134,175`
(post-`ollama create` is exit-code only, but it is first-run setup with no
rollback state to corrupt) and `selfmod._record_deployed_files:984` (records
the sha of what is on disk without comparing to the workspace source —
adequate because `_atomic_copy` is `copyfileobj` + `fsync` + `os.replace` and
`_verify_deployed_rollback` then executes the deployed bytes).
