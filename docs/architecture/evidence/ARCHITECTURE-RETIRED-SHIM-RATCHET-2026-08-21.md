# Retired package-path shim ratchet — 2026-08-21

The architecture checker now treats `sonder_runtime/adapters/ollama/gateway.py`
as a retired migration path while allowing only its exact, reviewed import shim
to remain during downstream caller migration. Any reintroduced or modified
implementation at that path is rejected as a retired-module violation.

Evidence:

- `scripts/check_architecture.py` has an explicit retired-path entry and an
  exact normalized-source allowlist for the one compatibility shim.
- `tests/production/test_architecture.py -k reintroduced_migrated_root` covers
  the full retired-path parameter set, including the Ollama gateway path.
- `python scripts/check_architecture.py` passes against the real tree.

This closes a policy/test inconsistency without promoting a formal checklist
requirement; the ledger remains `implemented_unverified` until the broader
architecture acceptance requirements are independently satisfied.
