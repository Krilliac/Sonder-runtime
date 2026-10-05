# Resource lease admission qualification

SQLite lease admission retries classified contention before the transaction
body. One monotonic ten-second budget selects the remaining native connection,
schema and `BEGIN IMMEDIATE` waits, with bounded backoff. It is a wait-selection
budget, not a hard cap on OS scheduling or cleanup time.

The connection factory closes failed setup handles. Failed cleanup produces a
distinct nonretryable error, preventing another connection from obscuring an
unresolved handle. Only numeric primary `SQLITE_BUSY` and `SQLITE_LOCKED` codes
(including extended codes) permit an admission retry after successful cleanup.
Message-only, unknown and non-contention errors remain dependency failures.

The database remains lazy, schema creation remains outside the transaction in
autocommit mode, and transaction bodies, commits, rollback behavior, owner
probes and cleanup evidence remain single-attempt. SQLite lock contention is
never presented as a conflicting resource lease without reading the lease.

## Reproduction and scope

Runtime PR #667's first hosted full-suite run failed the existing six-process
lease race: one winner, four busy refusals and one dependency refusal. That
test did not retain the failing SQLite statement, so its exact stage remains
unknown. A separate instrumented diagnostic recorded eleven WAL setup
`SQLITE_BUSY` failures among 192 children. That diagnostic retained objects;
it does not establish an ordinary-process failure rate or prove the hosted
failure's statement.

Thirteen new deterministic setup-cleanup and contention controls failed
against the unchanged published `20eae77` implementation before the repair.
The repaired focused set passed all 81 cases with zero skips, including
existing owner, TTL, reclaim, private SQLite and cached-connection contracts.
Controls also cover shrinking and exhausted admission budgets, rollback of
late admission, cleanup refusal and prevention of body/commit effect replay.

## Ordinary contention stress

Qualification ran 32 cold and 32 schema-warm races with six ordinary processes
per fresh database. All 384 children exited zero: 64 winners and 320 busy
refusals, with no dependency failures. Every database retained exactly the
correct winner's lease. All children were reaped, and the process supervisor
reported ECHILD with no cleanup signals. Independent read-only database
inspection verified all 64 persisted winners and unchanged raw artifact bytes.

The coordinator completed in 12.209 seconds. Round wall time was 167.138 ms
minimum, 187.760 ms median and 229.589 ms maximum. These measurements include
process imports, the start gate, scheduling, lease acquisition and persisted
readback. They describe synthetic local admission; they do not measure
provider throughput, model quality or improvement against an ordinary baseline.

## Complete local qualification

On 2026-10-05, the complete suite passed 25,515 tests, with 335 skips and four
passed subtests in 749.69 seconds. There were no failures or errors. All 335
skip identities and reasons remain in the raw report. All 74 source-derived
native controls passed exactly once: 70 PowerShell, three Go and one AF_UNIX.
All 51 OpenRouter gateway cases passed without skips.

Architecture, requirement/evidence, error, lint, documentation, history and
golden smoke/tool-policy gates passed. After the complete suite passed and
its owned children drained, the coordinator stopped before TUF because
temporary disk space was about 44 MiB below the unchanged 6 GiB preflight
floor. Its actual nonzero exit remains recorded. Only the verified successful
disposable full-suite fixture was removed; original logs, JUnit, source,
failed fixtures and unrelated data were retained.

The same qualified source then passed the two remaining stages: all 30 TUF
tests with zero skips in 2.80 seconds, and `git diff --check`. All 16 component
checks passed across these two phases. The full suite was not repeated and
no resource floor was waived. Both phases preserve explicit source hashes,
repository heads/statuses, the two protected user files and fixed
`HOME/.sonder` absence. Child supervisors reported ECHILD and absent owned
groups. This guard scope does not assert unchanged broad HOME or ignored files.

Content-free raw receipt SHA256 identifiers are:

- Ordinary stress result: `24770bad9f29c101065a0cd553187b225d9a25d08d06b182ac9990867c25c26e`.
- Independent stress audit: `3357cfe2e1f5eede0072eef27071046add85ecc54bbbc92cf9bcadf277ebb4f8`.
- Full-suite JUnit: `65c891619f9dfcdcde218155b1e9acfedf94fb2cf592aa9bc4cf02edc94e9b8a`.
- TUF JUnit: `ebc8ad495bb6a07579bc93e307e4bfd889d680efd1f67206161aa391f37a14a9`.
- Remaining-stage completion: `db69f6d97e8678c0171530fd5033cc72823d907043452e23b3241076786e8975`.
- Independent full/remaining audit: `9beb228fc485cda03ca3be7994b4b2fdd9e7244ffa9df1e6a0a3ddaec2aa38df`.

The [OpenRouter sanitation qualification](openrouter-stream-stability.md)
remains separate synthetic evidence. Its gateway and tests are byte-identical
to the previously qualified gateway revision. Merge additionally requires
every protected and applicable quality check to succeed on the exact final
published revision. Historical hosted failures remain retained.

Run the focused controls and complete qualification from the repository root:

```sh
python -m pytest -q tests/test_resource_leases.py tests/test_sqlite_factory.py tests/test_owned_sqlite.py tests/test_owned_sqlite_store_scopes.py
python -m pytest -q -n 4 --dist load --durations=25 -rs
python -m pytest -q -rs tests/production/test_tuf_publisher.py tests/test_update_manifest_trust.py tests/test_update_trusted_root.py
git diff --check
```

The architecture/evidence, documentation, privacy, lint and golden gates in
`CONTRIBUTING.md` are also required. A passing pytest run alone does not
replace those checks.
