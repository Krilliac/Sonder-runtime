# DOC-001 authority-map verification

DOC-001 requires `docs/architecture/README.md` to map authoritative, focused,
and historical documentation. [PR #536](https://github.com/Krilliac/Sonder-runtime/pull/536)
added a direct table linking the master requirement specification, all six
focused current contracts, and the three historical or superseded program
documents. `DOCUMENT-AUTHORITY-INDEX.md` remains the detailed companion.

The exact PR head `bc517fc614bc1f64d3f987354dbdbb81b77cc999` passed its
hosted tests, analysis, integrity, and platform checks. The merged source is
`58f89fa5719a20be132eb521bf4d1bf390aaf3a2`. At that SHA, [CI run
35912816718](https://github.com/Krilliac/Sonder-runtime/actions/runs/35912816718)
and [app-build run
35912816807](https://github.com/Krilliac/Sonder-runtime/actions/runs/35912816807)
both succeeded. On Windows, the merged checkout passed 12 focused tests in
`test_document_authority.py` and `test_remaining_doc_001_005.py`, plus the
documentation-authority, document-link, and requirement-evidence checks.

The row-aware test checks each README classification, direct link, and existing
target together. Generated focused-contract inventory and link checking guard
against missing targets. This verifies the repository authority map; it does
not claim that every sentence in a linked document is semantically current.
