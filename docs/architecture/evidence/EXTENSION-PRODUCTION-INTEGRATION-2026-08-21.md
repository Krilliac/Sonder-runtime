# Extension production integration slice — 2026-08-21

This bounded slice wires the existing extension contracts into the live
composition root. `SQLiteExtensionStateRepository` durably stores validated
installation records, including manifest digest, enabled state, health,
quarantine decision, and monotonic crash evidence. Reconstruction rejects
malformed or digest-mismatched state before publication. The production
bootstrap uses `extensions.db` and an empty provenance inventory by default,
so unlisted or unverified extensions remain disabled (fail closed).

The existing JSON-lines child host remains the only execution boundary. Its
startup/call deadlines, output-byte bound, restart budget, crash budget, and
no-replay behavior are unchanged. HTTP and CLI now expose explicit-authority
disable, enable, and repair operations; these operations never launch a
child. Existing experiment routes remain ephemeral and startup-authorized.

Verification:

```text
python -m pytest -q tests/test_extension_production_slice.py tests/test_extension_registry.py tests/test_extension_facade.py tests/test_extension_http_integration.py tests/test_extension_host.py tests/production/test_extension_composition.py
```

Limitations: artifact discovery, signature cryptography, native OS resource
limits, and promotion/install-from-source remain outside this slice. The
SQLite adapter persists registry state, not extension payloads or process
state. Evidence is implemented_unverified pending the broader architecture
and deployment gates.
