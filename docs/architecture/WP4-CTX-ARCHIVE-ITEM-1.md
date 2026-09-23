# Issue #510 item 1 — durable context archive and selective eviction

`SessionContextArchiveService` adds the first session-side context archive
slice. It treats the existing append-only `session_event` stream as the raw
archive and appends only `context.archive.created` metadata when a bulky
`tool.result` or `tool.completed` item is evicted. The metadata contains the
source event identity, byte count, and digest; it never copies tool output.

`prepare_context` removes the largest eligible tool results first until a byte
budget fits and returns model-visible placeholders containing a searchable
archive id. Request snapshots, user/model messages, and failure/decision
events are protected and remain in the retained view. `retrieve` reopens the
source event by sequence and verifies its identity and digest before returning
the original payload. `search` delegates to the repository's bounded search,
so failure history remains discoverable after restart.

The live HTTP context assembly currently constructs provider messages before
the capture service receives its request. This slice therefore exposes an
explicit, typed session-side seam and does not silently alter that assembly;
the next integration slice can call `prepare_context` immediately before
provider dispatch once its budget and context-item manifest are available.

Verification:

```text
python -m pytest -q tests/test_session_context_archive.py
```
