# REMAINING REPO-004/007 — optional LSP and multi-root boundaries

`sonder_runtime.application.repository_intelligence.lsp_multiroot` closes the
remaining navigation contract gap without coupling the application layer to a
particular language server or filesystem implementation.

## REPO-004

`LspNegotiator` selects an explicitly advertised LSP capability first, then an
indexed provider, and finally a lexical provider.  `query_with_fallback` only
invokes the selected provider and fails closed when that provider is absent.
The provider boundary is a protocol, so adapters own transport and lifecycle.

## REPO-007

`MultiRootReadContext` makes every visible root explicit and preserves each
root's independent Git revision.  Reads can span roots, but `authorize_write`
requires the context-selected root, its explicit owner, and the caller's
expected revision.  A stale revision or a non-owner cannot write another root.

## Evidence binding

`FileRevisionEvidence` binds a SHA-256 digest, root ID, and Git revision;
`bind_navigation_evidence` carries the root and revision into the existing
navigation evidence shape.  The module performs no filesystem, subprocess, or
network I/O. Evidence: `tests/test_remaining_repo_lsp_multiroot.py`, focused
tests, architecture/evidence gates, compileall, and diff checks.

