# WP1 Security Compatibility Alias Evidence — 2026-08-21

## Scope

The root modules `served_action_receipts.py` and `unsafe_lab.py` are retained
as legacy import paths. Both replace their entry in `sys.modules` with the
packaged adapter, so callers receive the canonical module object and preserve
monkeypatch/import identity semantics.

## Coverage added

`tests/test_security_compatibility_aliases.py` verifies:

- both root imports are identical to their packaged canonical modules;
- unsafe-lab activation is off by default and rejects abbreviated
  acknowledgements and privileged execution;
- non-loopback host exposure and cloud opt-in fail closed;
- successful activation writes one durable warning audit record and applies
  restrictive POSIX permissions.

## Verification

Focused command:

```text
python -m pytest tests/test_security_compatibility_aliases.py
```
