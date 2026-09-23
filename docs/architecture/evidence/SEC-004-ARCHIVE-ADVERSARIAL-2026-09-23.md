# SEC-004: package/archive safety under an adversarial corpus

SEC-004 requires every package and archive reader to bound files, expansion
ratios, paths, links, total bytes, and parser resource use. This change
inventories every production archive reader, fixes the defects that an
adversarial corpus exposed, and records the corpus as a regression gate.
The requirement-wide audit is in [SEC-AUDIT-2026-09-23.md](SEC-AUDIT-2026-09-23.md).

## Production archive readers

| Reader | Input trust | Bounds |
|---|---|---|
| `archive_list` / `archive_extract` (`sonder_runtime/adapters/inspection/archive_tools.py`) | untrusted, model or user supplied | entries, per-file bytes, aggregate bytes, per-entry and aggregate ratio, depth, path length, results, and time; portable names; no links, devices, setuid bits, encryption, or nested archives; duplicate and case-collision rejection; source-identity rechecks; staged, verified, no-replace promotion |
| Update staging `safe_extract` (`sonder_runtime/adapters/updates/service.py`) | trust-verified bundle, or unsigned behind two gates | traversal, drive-relative/UNC/backslash, alternate-stream, reserved-device, trailing dot/space, control characters, duplicate and case-colliding members, file-over-directory conflicts, links, devices, member count, and expanded bytes, all enforced for the whole archive before anything is written; the staged tree is then hash-verified against the manifest |
| `inspect_data` `.zip`/`.tar` previews (`sonder_runtime/adapters/filesystem/file_ops.py`) | authorized-root files | the ZIP central directory only; the TAR walk stops at a member bound and a decompression-scan bound, and reports counts as floors (`truncated`) |
| OOXML validation (`sonder_runtime/adapters/artifact_grounding.py` `_validate_ooxml`) | generated artifacts | entry bound, uncompressed-byte bound, safe unique paths, no encryption or links, and `testzip` only after the bounds pass |
| `adaptive_training.py` llama.cpp converter `extractall` | a `git archive` of a sealed tree pinned by SHA-256 | the tree hash is checked before archiving, and member paths and links are rejected before extraction |
| `sonder_runtime/application/security/path_archive_safety.py` | library contract (metadata only) | entry, byte, and path limits; links rejected |

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

## Evidence

`tests/test_sec004_archive_adversarial.py`: 25 cases across all three
untrusted readers, including the update-staging name corpus, duplicate and
case collisions, the member bound, validation before any write, hard links,
FIFOs, and block devices, overlapping ZIP entries, ZIP declared-size lies in
both directions, a deflate bomb stopped by the ratio check before
decompression, an early stop on the aggregate byte budget, and the preview
bounds.

RED/GREEN: run against the unchanged implementation, 14 of the 25 cases
failed. Every failure was one of the defects above. After the fix, all 25
pass. The other 11 cases already passed, which confirms that existing
protections (ratio, overlap, size lies, hard links and devices, the TAR entry
ceiling) are driven by a test.

Regression checks from the same worktree (Windows, Python 3.12.10):

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

Hosted CI: the pull request's exact-head run is recorded when the ledger
revision is marked `verified`.

## Limitations

The corpus is hand-built adversarial cases, not a coverage-guided fuzz
campaign; that belongs to SEC-008. Name portability is judged against
Windows, macOS, and Linux rules, not every filesystem. The update extractor
bounds members and bytes but has no wall-clock deadline of its own; it
relies on trust verification running first and on the manifest hash check
afterwards. The llama.cpp converter `extractall` reads only a sealed,
hash-pinned tree and was not re-hardened. Unicode-normalization collisions
(NFC and NFD) are not rejected up front. On normalizing filesystems,
`archive_extract` fails closed through exclusive creation and stage
verification, and update staging relies on manifest verification of the
staged tree.
