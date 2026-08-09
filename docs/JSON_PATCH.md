# Guarded JSON patch tool

`json_patch` previews or atomically applies a strict subset of RFC 6902 to one
existing UTF-8 JSON object or array. Supported operations are `add`, `remove`,
`replace`, and `test`; `move` and `copy` are intentionally unavailable.

```json
{
  "path": "config.json",
  "operations_json": [
    {"op": "test", "path": "/version", "value": 1},
    {"op": "replace", "path": "/version", "value": 2}
  ],
  "mode": "preview"
}
```

Use `preview` first to inspect the deterministic result without writing. Use
`apply` to commit the same operation list. Tests are exact and sequential: a
failed `test` aborts the transaction before disk mutation. JSON Pointer escapes
are strictly limited to `~0` and `~1`, and array indices reject leading zeros.

The tool refuses missing/non-regular targets, malformed or duplicate-key JSON,
non-finite numbers, sensitive/control-state paths, escapes from authorized
roots, and every symlink or junction component. Apply mode writes a
same-directory temporary file, rechecks target identity, uses atomic replace,
verifies the result, and atomically restores the original snapshot if a
post-replace failure occurs.

Hard ceilings are 256,000 bytes per source/result document, 128,000 bytes of
patch input, 100 operations, 64 JSON nesting levels, 64 pointer segments, and
384,000 bytes of returned JSON. Autopilot permits the tool only under workspace
policy, never observe-only policy. Project-bound agents rebase and confine the
single `path` to their host-selected project.
