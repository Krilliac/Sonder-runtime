# DOC-003 ADR-namespace verification

DOC-003 requires one namespace for new ADRs with globally unique IDs while
retaining the two numeric historical series. [PR
#539](https://github.com/Krilliac/Sonder-runtime/pull/539) made `docs/adr/`
the canonical directory, documented its dated-ID policy, froze the exact six
SPEC-5 numeric files there and nine architecture-program numeric files in
`docs/architecture/adr/`, and rejected newly numbered or misplaced records.
The checker also rejects invalid calendar dates and missing historical files.

The exact PR head `7b0fd01c1c7ea0eb8de697fcfef7da9233579627` passed
hosted tests, analysis, integrity, and platform checks. That implementation
merged as `fdd81abc356d341d9093f04f491dafbdb17a7603`. At that SHA, [CI run
35916199686](https://github.com/Krilliac/Sonder-runtime/actions/runs/35916199686)
and [app-build run
35916199676](https://github.com/Krilliac/Sonder-runtime/actions/runs/35916199676)
both succeeded.

Independent review found that nested ADR directories could bypass the
top-level filename check. [PR
#543](https://github.com/Krilliac/Sonder-runtime/pull/543) now rejects nested
directories and symlinks in both ADR series. Its exact head
`cebedb13c256470db186f6c66df964912a0f6a16` passed hosted tests,
analysis, integrity, and platform checks and merged as
`5c355622170bc545c71250cf47951d15d4e04e67`. At that SHA, [CI run
35920477611](https://github.com/Krilliac/Sonder-runtime/actions/runs/35920477611)
and [app-build run
35920477647](https://github.com/Krilliac/Sonder-runtime/actions/runs/35920477647)
both succeeded. The combined source passed 15 focused documentation tests and
the documentation-authority, link, and requirement-evidence checks on Windows.

New ADRs must use one date-prefixed filename directly in the canonical
directory, so the path and filename form one unique repository ID. The gate
preserves the historical paths; it does not judge ADR content or the semantics
of a claimed supersession.
