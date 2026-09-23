# DOC-002 historical-label verification

DOC-002 requires the SPEC-5 architecture, its migration runbook, and the old
program status to be labeled as superseded or historical while preserving
their decision history. Their labels and links to the active master checklist
were introduced by [PR #415](https://github.com/Krilliac/Sonder-runtime/pull/415)
at `1b7695c235509a43c5847d30bff8a81167fe1e08`; the three source
documents have not changed between that commit and
`fdd81abc356d341d9093f04f491dafbdb17a7603`.

The current `check_documentation_authority.py` directly inspects the opening
lines of all three documents for their historical/superseded label. The
authority index classifies each and retains its path. At the verified SHA,
[CI run 35916199686](https://github.com/Krilliac/Sonder-runtime/actions/runs/35916199686)
and [app-build run
35916199676](https://github.com/Krilliac/Sonder-runtime/actions/runs/35916199676)
both succeeded. The Windows documentation-authority and link checks passed.

This proof covers the three retained repository documents and their authority
labels. Older external links and copied historical text remain outside the
repository gate.
