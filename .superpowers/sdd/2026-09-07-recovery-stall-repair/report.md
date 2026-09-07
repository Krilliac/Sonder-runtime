# Recovery inventory-stall repair

Implementation commit: `d8bf21c3` (`fix(app-control): bound private inventory scans`)

Base examined: `bfc04fb462fdca5847208e21511c1beba8041ccd`.

## Diagnosis

The two recovery acceptances repeatedly entered the live app-control admission
path.  A single logical authorization rebuilt `live_control_plane_inventory()`
through nested configuration, catalog, grant, and model-root checks.  On
Windows, that inventory canonicalizes every control-plane path and SQLite
sidecar through `Path.resolve()`/metadata reads.  The repeated reconstruction,
not a deadlocked recovery executor, was the shared expensive path.

The HTTP acceptance has a deliberately coarse observation loop: every busy
poll sleeps ten seconds.  Its elapsed time is therefore not a direct measure
of a worker stall.  A duration probe after the repair recorded recovery
callbacks of 0.578s, 0.813s, 1.359s, 5.985s, and 17.125s; the test's polling
cadence accounted for most of its 76--90 second wall time.

## Repair and fences

`AppManagedAuthority` now obtains one fresh control-plane inventory at the
start of each app admission and carries it only through that admission.  It is
bound to the exact selection, context, and current account-signing-key digest.
Catalog/configuration reads accept that already validated snapshot, while the
existing account, source identity, selection, grant, lease, tool, and approval
checks remain live.

Three trusted lexical scopes reuse a snapshot only for their own operation:

* one app-control HTTP request;
* one owned prepared-work execution; and
* one non-closing explicit recovery callback.

The scope is a `ContextVar`, so it neither becomes a process-wide cache nor
crosses request/worker contexts accidentally.  Entry verifies that the
immutable inventory covers the private requirements.  Each use then requires
the same configuration object, exact normalized private requirement set, and
a digest of process inputs that select control-plane paths.  Every use still
checks current model-root disjointness.  A changed environment selector or
control source drops the scoped snapshot, rebuilds a fresh candidate, and
fails closed if it no longer covers the new requirement.  The recovery `close`
action intentionally remains outside this callback scope so local cleanup is
still possible after authority revocation.

No ownership, account approval, grant, migration, listener, or deployment
behavior was relaxed.  No snapshot is made available to a model, MCP surface,
or unrelated plugin.

## Regression evidence

New focused tests prove the call boundary and invalidation behavior:

* one fresh inventory per independent bound authorization;
* zero rebuilds for repeated checks inside an active owned scope and a rebuild
  immediately after it exits;
* scope invalidation on `SONDER_SYSTEM_PROFILE` change;
* control-source replacement rejects the stale scope after one fresh attempt;
* one explicit recovery callback enters/exits its owned scope exactly once,
  while `close` does not enter it; and
* the wire handler establishes one request scope before downstream work
  admission.

Validation from the inspection virtual environment:

```text
pytest six recovery files                                      39 passed in 175.16s
pytest app-control/managed-work/continuation focused files    136 passed in 161.46s
pytest six cache/fence regressions                               6 passed in 11.52s
py_compile changed Python files and git diff --check            passed
scripts/check_architecture.py                                   passed
scripts/check_error_signals.py                                  passed
scripts/check_history_privacy.py                                exit 0; known pre-existing history debt only
```

The original coordinator reproducer passed in 40.65s.  The HTTP reproducer
passed repeatedly after the final scope correction (76.48s and 90.38s); its
intentional ten-second busy-poll interval is the remaining wall-clock limit.

## Limits

This is a scoped repair, not a repository-wide green claim.  The full suite
was not run.  The inspection environment does not provide `ruff`, so Python
syntax checks, targeted regression suites, and the repository architecture,
privacy, and error-signal checks were used instead.  Nothing was pushed,
merged, deployed, or installed.
