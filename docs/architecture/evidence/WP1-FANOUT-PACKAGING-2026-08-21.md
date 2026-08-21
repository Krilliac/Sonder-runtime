# WP1 fanout persistence packaging checkpoint

Date: 2026-08-21  
Scope: durable fanout persistence, server ownership, compatibility imports,
architecture ratchets, and focused regression coverage.

## Ownership result

The canonical durable fanout receipt store now lives at
`sonder_runtime/adapters/persistence/fanout_store.py`. It retains the SQLite
schema, transaction helpers, leases, cancellation, result lifecycle, model
health, retry, encryption boundary, and retention behavior. The root
`fanout_store.py` is an identity compatibility redirect, preserving legacy
imports and private test seams. `server.py` imports the packaged adapter
directly.

## Verification

Focused commands:

```text
python -m pytest tests/test_fanout_store.py tests/test_fanout_policy.py tests/test_model_fanout.py --basetemp <host-temp>/pytest-fanout-wp1 -q
python -m pytest tests/test_fanout_store_compatibility.py --basetemp <host-temp>/pytest-fanout-compat-wp1 -q
```

Results: **275 passed, 1 warning** and **4 passed, 1 warning**. The warning
is the pre-existing repository `.pytest_cache` ACL warning on this Windows
workspace; test basetemps were kept outside the repository.

Additional ownership coverage proves:

- the packaged adapter owns the private schema and transaction seams;
- the root import resolves to the same module object;
- server binds the packaged adapter directly;
- the root module contains no implementation functions.

This remains an `implemented_unverified` ownership slice. Formal checklist
promotion and full-system deployment/receipt verification remain separate.
