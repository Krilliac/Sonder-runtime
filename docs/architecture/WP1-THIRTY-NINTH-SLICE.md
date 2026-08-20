# WP1 Thirty-Ninth Slice: Model-Inventory Presentation

Status: implemented on `agent/wp1-execution-status`.

## Scope

The pure Ollama inventory presentation helpers moved from the server
composition root to `sonder_runtime.adapters.model_inventory_formatting`:
canonical model-name extraction, casefolded inventory-name ordering, and
bounded CPU/GPU residency rendering. The original private `server` symbols
remain compatibility aliases. Inventory transport validation, model routing,
command catalog behavior, and persistence are unchanged.

## Evidence

- Model-inventory, model-fanout, and server-helper regressions plus direct
  adapter tests: **453 passed, 1 warning** (pytest cache permission warning).
- `python -m compileall -q server.py sonder_runtime tests`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --check`: passes.

## Remaining boundary

Inventory transport and capability/routing policy remain in the server and
are intentionally outside this presentation-only extraction.
