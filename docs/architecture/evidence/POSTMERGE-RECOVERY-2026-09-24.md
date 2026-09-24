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

PR #556 run 36047802011 at 1f54aabf failed both native HTTP variants. The stable
config variant reported a private-inventory coverage refusal during attachment.
The reconstructed config variant failed earlier, before workbench execution,
with CALLBACK_OUTCOME_UNKNOWN. The latter inner exception was not captured.

A local native trace then captured fleet.db-shm resolving to a volume-root
NTFS `$Extend/$Deleted` location while SQLite was deleting the sidecar. That
location is absent from the retained inventory, so coverage becomes false.
This demonstrates a real rejection mechanism at the same guard as hosted CI;
the hosted diagnostic does not identify its exact missing path. Microsoft
documents that POSIX-style Windows deletion removes the visible name while
existing handles remain valid ([deletion semantics](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information_ex)).

Canonicalization now retries the original path at most three times only when
Windows returns that local drive-root deleted-file namespace. A UNC share root
is not presumed to be an NTFS volume root. Every attempt uses
normal full resolution and existing namespace validation. A changed outside
target still fails inventory coverage; persistent tombstones raise. There is
no unresolved-path fallback, snapshot widening, or replay of an uncertain
callback. Nested ordinary directories and POSIX names remain ordinary paths.

Private inventory rejections log a fixed reason category, and recovery callback
logs retain only action, opaque attempt correlation and exception type.
Uncertainty is recorded before logging, so a broken diagnostic sink cannot
leave an accepted or replayable state. No failed callback is upgraded to success.

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

Prior retained-slice evidence at 1f54aabf: account-control 46 passed; real HTTP recovery
variants 2 passed; registry/drain cohort 18 passed; workflow pinning/privacy
15 passed. Six repository gates passed. Ruff comparison found 106 baseline
and 106 current findings with zero new diagnostic groups, not clean lint.

The full Linux run completed in 450.39 seconds with 17,505 passed, 167 skipped,
and one fixture-setup failure. The verifier's PYTHONPYCACHEPREFIX redirected
the bytecode that the selfmod fixture expects under its normal __pycache__.
That one test passed separately in 1.35 seconds with the prefix unset. These
are separate results, not a single green full-suite run. OPS-005 remains
implemented_unverified; the larger process-cleanup candidate is excluded from
all retained-slice claims.

The subsequent deleted-file-path correction has 65 native inventory/protection
regressions passing with one explicit POSIX-only skip. The initial deterministic
tests failed before the correction. A final read-only security review found no
blocker; changed-target, retry-bound, case/extended spelling and namespace
refusal tests cover the new behavior. Six repository gates and generated
catalog checks pass. The full native CI cohort passed 610 tests with 13 skips
before the final UNC exclusion; the 65-test cohort verifies that final change.
The final Linux run used the complete 1f54aabf tree plus this correction, with
3,541 source files matched after line-ending normalization and no copied bytecode.
It completed with 17,506 passed, 175 skipped and one smoke setup failure in
428.34 seconds: the Windows-source export had carried CRLF into release_smoke.sh.
The unchanged script was restored to its Git LF bytes, and that test passed
separately in 1.36 seconds. This is not a single green full-suite run. An earlier
agent run on b74e60ec omitted prior PR changes and is excluded from qualification.
Exact-head hosted results remain the final gate for this correction.
