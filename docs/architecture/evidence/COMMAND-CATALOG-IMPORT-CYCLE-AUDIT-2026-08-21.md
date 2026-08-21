# Command-catalog root/adapter import-cycle audit

Date: 2026-08-21

## Decision

Do not move `command_catalog.py` into `sonder_runtime/adapters` in this slice.
The smallest safe change is a typed, provider-neutral boundary: keep the
packaged adapter as a lazy compatibility provider and keep the root module as
the canonical implementation until its root dependencies have their own
packaged ports/providers.

## Evidence

- `server.py:149` imports
  `sonder_runtime.adapters.command_catalog.command_catalog` during server
  initialization.
- `sonder_runtime/adapters/command_catalog.py:6-8` resolves the root module
  only on attribute access with `importlib.import_module("command_catalog")`.
  It therefore does not import the root implementation while `server` is
  being initialized.
- `command_catalog.py:867`, `942`, and `1196` lazily import `server`; this is
  the existing reverse-edge cycle avoidance. Its `1022` lazy import of
  `permission_modes` is also explicitly documented as a reverse dependency.
- The root implementation is large and source-derived: it parses the dispatch
  source files and reads the live server tool registry and policy sets. Moving
  only the provider or changing the import direction would either leave the
  implementation root-owned or make the server import path depend on a module
  that asks for the still-initializing server.
- `sonder_runtime/application/ports/command_catalog.py` now contains only
  provider-neutral structural types (`CatalogParam`, `CatalogCommand`, the
  invocation tuple, and `CommandCatalog`). It imports neither side of the
  boundary.
- `sonder_runtime/adapters/command_catalog.py` explicitly implements that
  protocol and retains lazy root lookup only inside forwarding calls. The root
  `command_catalog.py` uses the same types only under `TYPE_CHECKING`, so the
  new seam adds no runtime import edge or cycle.
- The standalone `python scripts/check_architecture.py` completed successfully
  with no output.

## Verification

Focused command-catalog tests passed:

    81 passed in 5.49s

The requested architecture test file was also run. Its command-catalog and
architecture assertions did not expose a command-catalog cycle, but the full
file was not green because this checkout has unrelated pre-existing state:

- 100 passed;
- 1 failure: a broad architecture scan referenced missing root
  `pdf_risk.py`;
- 47 errors: pytest could not create its `tmp_path` fixtures because
  `C:\Users\Nathan\AppData\Local\Temp\pytest-of-Nathan` returned
  `WinError 5` (access denied).

The focused provider tests cover both structural implementations and the
existing lazy lookup/exception behavior.
