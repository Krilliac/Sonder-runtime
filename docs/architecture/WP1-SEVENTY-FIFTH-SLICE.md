# WP1 Seventy-Fifth Slice: logging platform seam

The filesystem workbench was the one safe packaged caller selected for this
slice. Its child-process environment helper now imports the canonical
`sonder_runtime.platform.logging` seam instead of importing the root
`sonder_logging` implementation directly.

The seam explicitly re-exports the existing formatter, handler setup,
redactor, redaction sentinel, and child-environment helper. This preserves
logging setup/handler behavior and secret/control-value redaction while the
implementation remains a compatibility-root module for other callers.

No server, persistence, command-catalog, launcher, HTTP/REPL, or strangler
service paths were changed.

## Evidence

- `tests/test_logging_platform_seam.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
