# SEC-004: package/archive safety under an adversarial corpus

SEC-004 requires every package and archive reader to bound files, expansion
ratios, paths, links, total bytes, and parser resource use. This change
inventories every production archive reader, fixes the defects that an
adversarial corpus exposed, and records the corpus as a regression gate.
The requirement-wide audit is in [SEC-AUDIT-2026-09-23.md](SEC-AUDIT-2026-09-23.md).

> **Status: verification withdrawn (ledger revision 4).** Revision 3 marked
> SEC-004 verified at `e077dbf2`. A re-review at `eca9f597` then measured
> unbounded parser work through GNU sparse members and uncaught recursion
> through chained metadata records, neither of which the metadata cap
> covered. The checkbox is cleared until those fixes have a green CI run on
> their exact SHA.

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

## Evidence

`tests/test_sec004_archive_adversarial.py`: 49 cases across all three
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
10 s.

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

- Round 2, after merging `main` (#525, #542): archive, update,
  data-inspect, update-engine, manifest-trust, artifact-grounding,
  architecture, and SEC-009 suites: 211 passed, 6 skipped;
  `scripts/check_architecture.py` passed.

Hosted CI history: run
[`35939556037`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35939556037)
passed at `e077dbf2`, which contained fixes 1 to 6. It was the basis of the
withdrawn revision 3. The CI receipt for fixes 7 to 10 is recorded with the
next verified ledger revision, which names its exact SHA.

## Limitations

The corpus is hand-built adversarial cases, not a coverage-guided fuzz
campaign; that belongs to SEC-008. Name portability is judged against
Windows, macOS, and Linux rules, not every filesystem. The update extractor
bounds members and bytes but has no wall-clock deadline of its own; it
relies on trust verification running first and on the manifest hash check
afterwards. The llama.cpp converter `extractall` reads only a sealed,
hash-pinned tree and was not re-hardened. Collision keys use NFC plus case
folding. That matches Windows, macOS, and default Linux behavior, but not
every filesystem's exact folding tables. The 64 KiB metadata cap, the
4-record chain cap, and the sparse rejection also reject legitimate archives
that use those features; Sonder's own bundles and archives use none of them.
The bounded TAR reader overrides private `tarfile.TarInfo` hooks
(`_proc_gnulong`, `_proc_pax`, `_proc_sparse`, `_proc_gnusparse_*`,
`_apply_pax_info`), and the ZIP pre-check uses `zipfile._EndRecData`. A
CPython change to those internals could bypass them, so the round-2 tests
must keep running on every supported interpreter. The ZIP bytes-per-entry
bound is a heuristic, and ordinary archives average far below it.
