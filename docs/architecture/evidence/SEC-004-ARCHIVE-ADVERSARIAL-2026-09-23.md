# SEC-004: package/archive safety under an adversarial corpus

SEC-004 requires every package and archive reader to bound files, expansion
ratios, paths, links, total bytes, and parser resource use. This change
inventories every production archive reader, fixes the defects that an
adversarial corpus exposed, and records the corpus as a regression gate.
The requirement-wide audit is in [SEC-AUDIT-2026-09-23.md](SEC-AUDIT-2026-09-23.md).

> **Status history.** Revision 3 marked SEC-004 verified at `e077dbf2`.
> Revision 4 withdrew that after a re-review at `eca9f597` measured
> unbounded parser work through GNU sparse members and uncaught recursion
> through chained metadata records. Revision 5 re-verifies at `cf83cb18`,
> which contains fixes 1 to 10 below. The CI receipt is under Evidence.
> Revision 6 withdrew that again after a round-3 re-review at `774c8965`
> measured global PAX (`g`) keys accumulating into every later member and
> per-member PAX (`x`) keys retained per member without an archive-wide
> budget. Revision 7 re-verifies at `4d689fee`, which contains fixes 1 to
> 13. The CI receipt is under Evidence.

## Production archive readers

| Reader | Input trust | Bounds |
|---|---|---|
| `archive_list` / `archive_extract` (`sonder_runtime/adapters/inspection/archive_tools.py`) | untrusted, model or user supplied | entries, per-file bytes, aggregate bytes, per-entry and aggregate ratio, depth, path length, results, and time; portable names; no links, devices, setuid bits, encryption, or nested archives; duplicate and case-collision rejection; source-identity rechecks; staged, verified, no-replace promotion |
| Update staging `safe_extract` (`sonder_runtime/adapters/updates/service.py`) | trust-verified bundle, or unsigned behind two gates | traversal, drive-relative/UNC/backslash, alternate-stream, reserved-device, trailing dot/space, control characters, duplicate and case-colliding members, file-over-directory conflicts, links, devices, member count, and expanded bytes, all enforced for the whole archive before anything is written; the staged tree is then hash-verified against the manifest |
| `inspect_data` `.zip`/`.tar` previews (`sonder_runtime/adapters/filesystem/file_ops.py`) | authorized-root files | the ZIP central directory only; the TAR walk stops at a member bound and a decompression-scan bound, and reports counts as floors (`truncated`) |
| OOXML validation (`sonder_runtime/adapters/artifact_grounding.py` `_validate_ooxml`) | generated artifacts | entry bound, uncompressed-byte bound, safe unique paths, no encryption or links, and `testzip` only after the bounds pass |
| `adaptive_training.py` llama.cpp converter `extractall` | a `git archive` of a sealed tree pinned by SHA-256 | the tree hash is checked before archiving, and member paths and links are rejected before extraction |
| `sonder_runtime/application/security/path_archive_safety.py` | library contract (metadata only) | entry, byte, and path limits; links rejected; TAR through the bounded reader and ZIP entry count checked from the end-of-central-directory record (round 2) |

## Defects found and fixed

1. **Update staging accepted non-portable member names.** `C:x`, `a:b`, `CON`,
   `nul.txt`, and `trailing. ` passed the old `Path(name)` check. On Windows,
   `dest / "C:x"` leaves the staging directory when the drives differ, and `a:b`
   writes an NTFS alternate data stream onto another file. These names are now
   rejected on every host.
2. **Update staging had no member-count bound and wrote before it validated.**
   Duplicate and case-colliding members silently replaced earlier ones, and a
   hostile member late in the archive was only found after earlier members had
   been written. `_plan_members` now validates every member, the member count
   (`max_members`, default 50,000), and the aggregate size before
   `_extract_members` writes anything.
3. **The `archive_list`/`archive_extract` TAR walk overran its budget.** The
   aggregate byte ceiling was checked only after every header had been walked.
   Reaching each header of a compressed TAR decompresses the previous payload,
   so the parser could do up to entries × per-file-limit of work before
   rejecting. The walk now stops at the first member that exceeds the
   aggregate.
4. **`.tar` previews had no bound.** `getmembers()` decompressed and collected
   the whole archive, and the member count was reported as a total. The
   preview now iterates under `INSPECT_MAX_ARCHIVE_MEMBERS` and
   `INSPECT_MAX_ARCHIVE_SCAN_BYTES`, never peeks past a bound, and sets
   `truncated` when the counts are floors.
5. **Header metadata was allocated before any limit ran** (review round 1).
   `tarfile` reads a GNU long-name (`L`) or long-link (`K`) record, or a PAX
   (`x`/`g`) header, with a single `read(size)` of the size the record
   declares. A few hundred KiB of `.tar.gz` could therefore make every TAR
   reader allocate hundreds of MiB while parsing one header, before the
   entry, byte, or ratio checks of fixes 2 to 4 could run. Those checks
   bound member *payloads*, not header metadata. All three readers, and the
   `archive_tools` format probe that replaced `tarfile.is_tarfile`, now open
   archives through a bounded reader (now
   `sonder_runtime/application/security/bounded_archives.py`). It rejects any
   such record that declares more than 64 KiB (`MAX_TAR_METADATA_BYTES`)
   before reading its payload.
6. **Device names and normalization collisions** (review round 1). Both
   extractors now also reject `CONIN$`, `CONOUT$`, `COM0`/`LPT0`,
   superscript `COM¹`–`COM³`/`LPT¹`–`LPT³`, and device stems followed by
   spaces (`CON .txt`). Duplicate and collision detection keys are
   NFC-normalized as well as case-folded, so `café` in NFC and in NFD
   collide as they would on a normalizing filesystem.
7. **GNU sparse members made the parser do unbounded work** (review round 2).
   Round 1 capped the size of long-name and PAX records, but not sparse
   processing. For an old-style `S` member, `tarfile` follows a chain of
   512-byte extension blocks. For a PAX `GNU.sparse.*` member, format 1.0
   stores the sparse map in the member data and `tarfile` reads as many map
   entries as the header declares. `safe_extract` and the preview accepted
   such archives. `archive_list` rejected them only after spending the memory
   and time. `BoundedTarInfo` now rejects `S` members, the three PAX sparse
   formats (before format 1.0 reads its map), and any `GNU.sparse.*` key in
   a local or global PAX header. No Sonder reader needs sparse files.
8. **Chained metadata records recursed without limit** (review round 2).
   `tarfile` handles each long-name, long-link, or PAX record by recursing
   into the next header. 3,000 small chained records raised an uncaught
   `RecursionError` in every reader. At most 4
   (`MAX_TAR_METADATA_CHAIN`) metadata records may now precede one member.
   Violations surface as `TarMetadataLimitError` (a `TarError`), which each
   reader maps to its own rejection: `ArchiveRejected`, `ExtractionError`,
   an invalid listing, or a preview error.
9. **ZIP readers built the whole entry list before counting** (review round
   2). `zipfile.ZipFile` creates one `ZipInfo` per central-directory record
   when it opens. `archive_tools`, the `inspect_data` preview, OOXML
   validation, and `path_archive_safety.inspect_zip` now read the
   end-of-central-directory record first and compare the declared entry
   count against their limit before opening. The bounded paths also compare
   the central-directory size against 4 KiB per declared entry. The preview
   reports the declared count as `truncated` rather than parsing it.
10. **`path_archive_safety.inspect_tar`** used raw `tarfile.open` and
    `getmembers()`. It now iterates through the bounded reader, so its entry
    limit stops the walk.
11. **Global PAX records accumulated** (review round 3). `tarfile` merges
    every global PAX (`g`) record into the archive's `pax_headers` and copies
    the merged mapping into each later member, which stays alive in
    `TarFile.members`. Retained memory grows quadratically: the reviewer
    measured 12.5 GiB from a 3.7 MiB `.tar.gz` with 500 members.
    `safe_extract`, the preview, and `path_archive_safety.inspect_tar`
    accepted such archives; only `archive_tools` rejected them, through its
    own separate check. `BoundedTarInfo` now rejects a `g` record before
    reading its payload.
12. **Per-member metadata had no archive-wide budget** (review round 3).
    Every member's PAX keys are retained for the life of the archive object.
    All long-name, long-link, and PAX records in one archive now share
    1 MiB of declared payload (`MAX_TAR_METADATA_TOTAL_BYTES`) and 32,768
    parsed PAX keys (`MAX_TAR_PAX_KEYS`).
13. **Two ZIP readers checked only the declared count** (review round 3).
    The `inspect_data` preview and OOXML validation now also apply
    `require_zip_entry_bound`, which checks the central-directory size, as
    `archive_tools` and `path_archive_safety` already did.
14. **Clear rejection messages and a staging deadline** (review round 4;
    the bounds themselves are unchanged). Each reader now reports a
    metadata-bound violation as its own rejection with a clear message:
    - `safe_extract` raises `ExtractionError` ("archive metadata exceeds
      reader bound") instead of "corrupt or truncated archive";
    - the `inspect_data` preview raises `ArchivePreviewRejected` for TAR
      metadata and for oversized ZIP central directories;
    - `path_archive_safety.inspect_tar` raises `ArchiveLimitError`;
    - `archive_tools` already raised `ArchiveRejected`.

    `safe_extract` also takes `max_seconds` (default 300 s). It checks the
    deadline before each member in both the validation walk and the write
    loop, because walking the 50,000-member cap can cost tens of seconds of
    CPU.

## Evidence

`tests/test_sec004_archive_adversarial.py`: 59 cases across all three
untrusted readers, including the update-staging name corpus, duplicate and
case collisions, the member bound, validation before any write, hard links,
FIFOs, and block devices, overlapping ZIP entries, ZIP declared-size lies in
both directions, a deflate bomb stopped by the ratio check before
decompression, an early stop on the aggregate byte budget, the preview
bounds, oversized `L`/`K`/`x` metadata in every reader (peak traced memory
must stay under 8 MiB for a 48 MiB declared record, with the fixture
generated in-test at under 512 KiB), the additional device names, NFC/NFD
collisions, case-folded component collisions (`Dir/a` with `dir/b`), and a
file that is an ancestor of another member, in either order. Round 2 adds
old-style and PAX 1.0 sparse archives, each generated in-test at under
64 KiB; each `GNU.sparse` key in local and global PAX headers; 3,000-record
`L`, `K`, and `g` chains; a check that normal PAX and GNU long names still
parse; the ZIP central-directory count checked with `_RealGetContents`
instrumented so the test fails if a reader parses the directory first; and
`path_archive_safety.inspect_tar`. Every rejection case asserts the typed
error from each reader, peak traced memory under 8 MiB, and wall time under
10 s. Round 3 adds a 16-member corpus with a 60 KiB distinct-key global
record before each member, which must be rejected with peak memory under
2 MiB, well below one member's worth of accumulation. It also adds 40-member
corpora with 60 KiB of per-member PAX keys, both many small keys and fewer
large ones; a check that 200 ordinary PAX long-name members still fit the
budget; and a ZIP with a small declared count but a large central directory.
Test sizes stay small; the tests assert early rejection rather than
allocating the reviewer's gigabytes.

RED/GREEN, round 0: run against the unchanged implementation, 14 of the
first 25 cases failed, each on one of defects 1 to 4. After the fix, all 25
pass. The other 11 already passed, which confirms that existing protections
(ratio, overlap, size lies, hard links and devices, the TAR entry ceiling)
are driven by a test.

RED/GREEN, review round 1: of the 12 cases added, 9 failed before fixes 5
and 6 (three metadata types, five device names, NFC collision). All 37 pass
after them. The 3 component and ancestor cases passed on the round-0 code;
they were added because nothing tested those protections before.

RED/GREEN, review round 2: all 12 new cases failed on `eca9f597`. The
reasons: sparse archives accepted, `RecursionError`, an uncaught
`ValueError` from a malformed `GNU.sparse.map`, the ZIP central directory
parsed first, and `path_archive_safety` unbounded. All 49 pass after fixes
7 to 10. Measured with the same generated fixtures (Windows, Python 3.12,
`tracemalloc` peak):

| Fixture (compressed size) | Reader | Before (`eca9f597`) | After |
|---|---|---|---|
| old GNU sparse, 4,000 extension blocks (8.9 KiB) | `archive_list` | 5.3 MiB, 0.95 s, rejected late | 0.2 MiB, 0.07 s, rejected |
| | `safe_extract` | 17.1 MiB, 1.09 s, **accepted** | 0.2 MiB, <0.01 s, `ExtractionError` |
| | preview | 5.1 MiB, 0.42 s, **accepted** | 0.2 MiB, <0.01 s, rejected |
| PAX `GNU.sparse` 1.0, 2 M map numbers (4.0 KiB) | `archive_list` | 93.1 MiB, 14.84 s, rejected late | 0.2 MiB, 0.06 s, rejected |
| | `safe_extract` | 168.5 MiB, 8.68 s, **accepted** | 0.2 MiB, <0.01 s, `ExtractionError` |
| | preview | 93.0 MiB, 7.10 s, **accepted** | 0.2 MiB, <0.01 s, rejected |
| 3,000 chained `L` records (12.7 KiB) | all three | 0.7 MiB, **`RecursionError`** | 0.2 MiB, typed rejection |
| 3,000 chained `g` records (12.6 KiB) | all three | 0.8 MiB, **`RecursionError`** | 0.2 MiB, typed rejection |

Growth is linear in the declared map length. The reviewer's larger fixtures
reached 0.5 to 3.3 GiB and 40 to 150 s before the fix. The old-style sparse
fixture stays under the 8 MiB budget even before the fix, so for that case
the test fails on acceptance, not on memory.

RED/GREEN, review round 3: the 4 new rejection cases failed on
`774c8965` because the archives were accepted or the ZIP was parsed. All 54
pass after fixes 11 to 13. Before (`774c8965`) and after, same fixtures:

| Fixture (compressed size) | Readers | Before | After |
|---|---|---|---|
| 16 members, 60 KiB distinct global keys each (96 KiB) | `safe_extract` / preview / `path_archive_safety` | 14.8 MiB, 0.5 s, **accepted** | 0.3 MiB, <0.01 s, rejected |
| 32 members, same (192 KiB) | same | 48.0 MiB, 1.2 s, **accepted** (quadratic) | 0.3 MiB, <0.01 s, rejected |
| 40 members, 60 KiB per-member keys, 8-byte values (290 KiB) | same | 15.7 MiB, 1.2 s, **accepted** | 5.3 MiB, 0.36 s, rejected at the key cap |
| 40 members, 60 KiB per-member keys, 200-byte values (37 KiB) | same | 3.7 MiB, 0.16 s, **accepted** | 1.7 MiB, 0.06 s, rejected at the byte budget |
| ZIP, 8 entries with 60 KB comments (469 KiB) | preview | parsed and **accepted** | rejected from the end record, directory not parsed |

`archive_tools` already rejected every one of these through its own
checks, at 0.1 to 1.1 MiB.

Regression checks from the same worktree (Windows, Python 3.12.10), round 0:

- `tests/test_archive_tools.py`, `tests/production/test_updates.py`,
  `tests/test_data_inspect.py`, `tests/test_wp9_path_archive_safety.py`,
  `tests/test_archive_extract_executor.py`,
  `tests/test_archive_create_boundary.py`,
  `tests/test_archive_create_executor.py`: 84 passed.
- `tests/production/test_update_engine.py`,
  `tests/production/test_tuf_publisher.py`,
  `tests/test_update_engine_version_boundary.py`,
  `tests/test_update_manifest_trust.py`, `tests/test_path_portability.py`,
  `tests/test_backup_service.py`, `tests/production/test_architecture.py`:
  99 passed, 7 skipped (`tuf` is not installed locally).
- Round 1, after merging `main`: archive, update, data-inspect, update-engine,
  manifest-trust, and architecture suites: 158 passed, 6 skipped;
  `scripts/check_architecture.py` passed.

- Round 4: the 5 new message and deadline cases failed before fix 14
  and pass after it. The earlier-round cases now assert the reader-specific
  rejection types; 59 of 59 pass. SEC-004 revision 7 still holds, because
  fix 14 changes no bound.
- Round 3: archive, update, data-inspect, update-engine,
  manifest-trust, artifact-grounding, architecture, SEC-004, and SEC-009
  suites: 265 passed, 6 skipped; `scripts/check_architecture.py` passed.
- Round 2, after merging `main` (#525, #542): archive, update,
  data-inspect, update-engine, manifest-trust, artifact-grounding,
  architecture, and SEC-009 suites: 211 passed, 6 skipped;
  `scripts/check_architecture.py` passed.

Hosted CI history: run
[`35939556037`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35939556037)
passed at `e077dbf2`, which contained fixes 1 to 6. It was the basis of the
withdrawn revision 3. The CI receipt for fixes 7 to 10 is recorded with the
next verified ledger revision, which names its exact SHA.

Re-verification receipt (revision 7): the pull request's exact-head run
[`35951205702`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35951205702)
passed its `Validate master-spec evidence ledger`,
`Validate evidence changes against pull request base`, and `Run test suite`
steps (Ubuntu, 16883 passed, 125 skipped) at `4d689fee91980fecf6686aad7e367b79beeee423`. That head contains fixes
1 to 13, all 54 corpus cases, and the merge of `main` through #519 and #552.
The ledger's `verified_sha` names it. The checkbox and this receipt follow in
a later commit, which is gated by CI on the pull request's final head.

Superseded receipt (revision 5): the pull request's exact-head run
[`35945618003`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35945618003)
passed its `Validate master-spec evidence ledger`,
`Validate evidence changes against pull request base`, and `Run test suite`
steps (Ubuntu, 16721 passed, 104 skipped) at
`cf83cb184981db46692234b8f1f36e2559410858`. That head contains fixes 1 to 10 and all 49 corpus cases. The
ledger's `verified_sha` names it. The checkbox, this receipt, and a merge
of `main` (#541, #549) follow in later commits, which are gated by CI on the
pull request's final head.

## Limitations

The corpus is hand-built adversarial cases, not a coverage-guided fuzz
campaign; that belongs to SEC-008. Name portability is judged against
Windows, macOS, and Linux rules, not every filesystem. The update extractor
bounds members, bytes, and wall time (default 300 s). It relies on trust
verification running first and on the manifest hash check afterwards. The
revision-7 ledger limitation that says it has no deadline predates fix 14. The llama.cpp converter `extractall` reads only a sealed,
hash-pinned tree and was not re-hardened. Collision keys use NFC plus case
folding. That matches Windows, macOS, and default Linux behavior, but not
every filesystem's exact folding tables. The 64 KiB metadata cap, the
4-record chain cap, the 1 MiB / 32,768-key archive-wide metadata budget, and
the rejection of sparse members and global PAX headers also reject
legitimate archives that use those features. For example, `git archive`
tarballs carry a global PAX comment, and archives with several thousand
long-named members exceed the budget. Sonder's own bundles use none of
these.
The bounded TAR reader overrides private `tarfile.TarInfo` hooks
(`_proc_gnulong`, `_proc_pax`, `_proc_sparse`, `_proc_gnusparse_*`,
`_apply_pax_info`), and the ZIP pre-check uses `zipfile._EndRecData`. A
CPython change to those internals could bypass them, so the round-2 tests
must keep running on every supported interpreter. The ZIP bytes-per-entry
bound is a heuristic, and ordinary archives average far below it.
