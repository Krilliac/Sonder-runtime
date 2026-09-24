# DOC-007 product status-vocabulary verification

DOC-007 requires product documentation to distinguish implemented,
experimental, proposed, degraded, and unsupported behavior.
[PR #549](https://github.com/Krilliac/Sonder-runtime/pull/549) implements
that contract at commit `27a476f7eb2a67eaa6fa05de23035e183ffeeeff` on
baseline `494f2397601b784abe82d5131693f16baad709d4`.

## What changed

- `DOCUMENT-AUTHORITY-INDEX.md` defines product documentation as the root
  `README.md` plus the six focused contracts. It also defines the five
  labels: Implemented, Experimental, Proposed, Degraded, and Unsupported.
- All seven product documents carry one `## Behavior status` table
  (`Behavior | Status | Boundary`). All five labels are used. Examples include
  the `app-latest` prerelease and unsafe lab mode (Experimental); `script_run`
  `deny-*` modes and AMD VitisAI (Degraded); and wake-on-LAN, CPU-offloaded
  training, and model-weight sharding (Unsupported).
- The root README had 159 lines of WP1 implementation-slice notes appended
  after its last section. They were moved verbatim into
  `docs/architecture/WP1-README-SLICE-LOG.md`, which is classified as
  implementation history. The README now describes current behavior only, and
  a soft promise about speech and reranker tags was restated.
- `scripts/check_documentation_authority.py` rejects unknown labels, a missing
  or duplicated status section, and a label vocabulary that no product
  document uses. It also rejects `Proposed` rows without an open requirement.
  Outside the status table, it rejects forward-looking phrases such as
  "coming soon", "in a future", "future backend", "not yet available", TODO,
  and TBD, plus WP slice-log lines.

## Verification

At `27a476f7`, on Windows with Python 3.12.10, the documentation-authority,
document-link, evidence-document, architecture, and requirement-evidence
(`--base-ref origin/main`) checks exited 0. The documentation pytest suites
passed 28 tests, including six negative-case fixtures for this gate.

When the new checker was run against the baseline README and the six focused
contracts, it reported 168 problems. These included 149 slice-log lines, seven
missing status tables, two unlabeled future-tense phrases, and all five labels
unused.

Hosted CI for the exact implementation head:
[CI run 35932793054](https://github.com/Krilliac/Sonder-runtime/actions/runs/35932793054)
succeeded on Ubuntu at `27a476f7`, including requirement evidence, doc links,
generated-reference freshness, documentation authority, and the full test
suite (16471 passed, 104 skipped). The paired app-build run 35932792997 was still queued when this record
was written, and it is not claimed here.

## Review correction and re-verification

Review of the first verified revision found that `CLIENT.md` described
`sonder_client.py` as a standalone, stdlib-only, single-file download.
Since `309f46f2`, the file imports six `sonder_runtime` client adapters, so a
lone copy fails with `ModuleNotFoundError`. The gate at `27a476f7` checked
structure and vocabulary, so it could not detect that false Implemented row.

Commit `cb1a9a22` made these corrections:

- `CLIENT.md` now documents the checkout requirement and has an Unsupported
  row for single-file copies. `sonder_client.py` examples use https.
- `test_thin_client_documentation_matches_its_checkout_dependency` proves the
  dependency both ways: a lone copy fails, and a checkout imports.
- The underived "46 direct MCP call paths" count was removed.
- The gate now has wider stale-promise and slice-log patterns.
- Implemented rows must cite IDs whose latest ledger status is at least
  implemented_unverified.
- A planted-document test goes through `check()`. It was mutation-verified:
  unwiring the product-document gate makes it fail.
- A four-column-row test covers the column check.
- `scripts/check_doc_links.py` now scans the 151 evidence records under
  `docs/architecture/evidence/`. Those records currently contain no relative
  Markdown links: they cite files as backticked paths and use absolute web
  links. The scan therefore verifies nothing today and only guards relative
  links added later. Earlier "doc links passed" claims never covered evidence
  records at all.

The earlier verified revision is superseded by one bound to `cb1a9a22`:
[CI run 35938479949](https://github.com/Krilliac/Sonder-runtime/actions/runs/35938479949)
succeeded on Ubuntu at `cb1a9a22`. That covers requirement evidence, doc links
(the evidence-folder scan, which found no relative links to check), generated-reference freshness,
documentation authority, and the full test suite (16481 passed, 104 skipped).
The paired app-build runs were still queued, so they are not claimed.

## Limitations

The vocabulary gate covers the root README and the six focused contracts. It
does not cover wiki pages, runbooks, `NPU.md`, or external copies. It checks
labels and phrasing, not the semantic truth of each row, so row accuracy
remains review work. The evidence-folder link scan is currently vacuous:
evidence records contain no relative Markdown links, and the gate does not
resolve backticked repository paths. Of 362 such paths, 23 do not resolve;
most are package-relative shorthand or files removed since. These remain
unchecked follow-up work.
