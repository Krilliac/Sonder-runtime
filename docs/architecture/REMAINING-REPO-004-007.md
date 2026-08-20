# REMAINING REPO-004/007 — optional LSP and multi-root boundaries

`sonder_runtime.application.repository_intelligence.lsp_multiroot` closes the
remaining navigation contract gap without coupling the application layer to a
particular language server, filesystem implementation, or repository format.

## REPO-004

`LspNegotiator` selects an explicitly advertised LSP capability first, then an
indexed provider, and finally a lexical provider.  `LspTransport` and
`LspSession` are provider-neutral live-session ports: `open_live_lsp` binds a
session to one visible root, applies a result bound, validates returned root
identity, and closes the adapter-owned session deterministically.  The
application layer performs no network or subprocess I/O; an adapter supplies
the transport.  `query_with_fallback` remains the fail-closed static seam.

## REPO-007

`MultiRootReadContext` makes every visible root explicit and preserves each
root's independent Git revision.  `RepositoryNavigationPort` is the
read-only adapter port used by `MultiRepositoryNavigator`; it coordinates
bounded queries across multiple repository instances while retaining each
root's revision and rejecting cross-root evidence.  Reads can span roots, but
`authorize_write` requires the context-selected root, its explicit owner, and
the caller's expected revision.  A stale revision or a non-owner cannot write
another root.  No repository is mutated.

## Evidence binding

`FileRevisionEvidence` binds a SHA-256 digest, root ID, and Git revision;
`bind_navigation_evidence` carries the root and revision into the existing
navigation evidence shape. Evidence: `tests/test_remaining_repo_lsp_multiroot.py`
covers live session lifecycle, bounded results, multi-repository reads, root
identity rejection, independent revisions, and write authorization. The
module performs no filesystem, subprocess, or network I/O. Architecture,
evidence, compileall, and diff checks are required before promotion.
