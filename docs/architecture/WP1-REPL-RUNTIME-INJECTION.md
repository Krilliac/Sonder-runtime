# WP1 — REPL runtime injection

`sonder_runtime.interfaces.repl.repl` no longer imports the legacy `server`
root. The interface retains its established command branches through a single
late-bound proxy, while composition supplies the runtime explicitly with
`configure_legacy_runtime(runtime)`.

The proxy is fail-closed: an unconfigured REPL raises the typed
`DependencyUnavailable` error, and an injected object missing a requested
runtime member raises the same error. There is no `importlib`, module lookup,
or fallback discovery in this boundary. The live-reload module list contains
only the explicitly reloadable non-runtime helpers and cannot replace the
injected runtime behind the caller's back.

Validation:

```text
python -m pytest tests/test_wp1_repl_runtime_injection.py -q
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

This slice does not modify `__main__.py`, HTTP interfaces, or formal
implementation-spec checkboxes.
