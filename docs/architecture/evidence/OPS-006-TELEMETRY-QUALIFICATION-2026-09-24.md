# OPS-006 bounded telemetry qualification

This focused qualification exercises the production local activity telemetry
producer and snapshot consumers in
`sonder_runtime.adapters.observability.activity_tracker`. It does not send
data to an external exporter and uses synthetic sentinel values only.

Command, serial with isolated pytest basetemp:

```text
python -m pytest -q tests/test_ops006_telemetry_qualification.py --basetemp=<isolated-temp>
```

Result: **2 passed in 1.78s**; Ruff is clean for the new test.

The first test records a real response span, model call, tool result, and file
change through the production recording APIs. The default `public_snapshot`
and `execution_feed` projections contain none of the synthetic prompt, secret,
or artifact sentinels and remain within the production event and byte limits.

The second test drives 52 distinct owner labels and 17 unique event labels per
owner plus 17 repeated spans for the latest owner through the production response/feed APIs. The owner-scoped feed retains
exactly `MAX_OWNER_FEED_ENTRIES` after overflow; only `MAX_FEED_OWNERS` histories remain and the oldest owner is evicted; the global execution feed remains capped
at `MAX_FEED_EVENTS` and `MAX_FEED_BYTES`, reports truncation, and does not
expose the synthetic secret sentinel.

This qualifies a bounded local diagnostic projection. It does not establish
external exporter redaction, retention/deletion policy, deployment transport,
or cardinality behavior in an external metrics backend. The requirement remains implemented_unverified; no master checkbox is promoted.
