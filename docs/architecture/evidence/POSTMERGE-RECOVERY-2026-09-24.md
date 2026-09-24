# Post-merge recovery and cleanup qualification

Post-merge CI run 36027606317 at b74e60ec passed Linux tests but failed the
native Windows recovery attachment test: approval was expected, while the
callback reported ACTION_OUTCOME_UNKNOWN. The diagnostic-only isolation probe
also failed separately; its failure is not production Windows isolation proof.

A deterministic regression establishes a distinct private-inventory defect:
rebuilding a value-equal immutable config while expanding requirements retires
an existing admission lease. The stale-scope check now applies the same value
equality policy as record lookup. Changed configuration, scope, issuer,
coverage, and copied-context refusals remain intact. The real loopback recovery
fixture now covers both stable and reconstructed equal configs.

The original intermittent CI exception has not been reproduced with a decisive
underlying category. Some local runs failed during terminal projection
revalidation; subsequent instrumented runs passed. The patch does not label
this nondeterminism solved merely because a retry passed. Private inventory
rejections now log a fixed reason category, and recovery callback logs retain
only action, opaque attempt correlation and exception type. Uncertainty is
recorded before logging, so a broken diagnostic sink cannot leave an accepted
or replayable state. No failed callback is upgraded to success.

The larger process-identity/group-confirmation candidate is not included.
Its full Linux run stopped after 11,507 passes and five provider/restart
failures. Identity-less fixtures and coordinated cancellation/reaping need
more work before that candidate can be integrated. In particular, a cleanup
signal is not proof of tree completion, and a cancelled process must not be
misreported as failed merely because it exits during cleanup. The candidate
is retained separately with those failing results, not marked complete.

This patch retains only the independent finite-drain-deadline validation:
NaN, infinity, booleans, non-numeric and non-positive deadlines are refused.
No process termination authority or cleanup-completion contract changes here.

The required GitHub `tests` context now depends on native Windows and the
reusable four-probe Linux container qualification. Failure, cancellation or
skipping either prerequisite makes the required context fail. Required job
names are preserved. The standalone container workflow remains manually
callable, while CI owns automatic invocation to avoid duplicate runs.

The existing GitHub-token merge automation does not generate push-triggered
post-merge runs. Explicit post-merge workflow dispatch remains necessary; this
patch does not introduce additional token permissions or a new merge bot.

Local retained-slice evidence: account-control 46 passed; real HTTP recovery
variants 2 passed; registry/drain cohort 18 passed; workflow pinning/privacy
15 passed. Six repository gates passed. Ruff comparison found 106 baseline
and 106 current findings with zero new diagnostic groups, not clean lint.

The full Linux run completed in 450.39 seconds with 17,505 passed, 167 skipped,
and one fixture-setup failure. The verifier's PYTHONPYCACHEPREFIX redirected
the bytecode that the selfmod fixture expects under its normal __pycache__.
That one test passed separately in 1.35 seconds with the prefix unset. These
are separate results, not a single green full-suite run. Exact-head hosted
qualification remains pending. OPS-005 remains implemented_unverified; the
larger process-cleanup candidate is excluded from all retained-slice claims.
