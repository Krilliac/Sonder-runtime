# Research port: stream-JSON output projection

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Zero documents a headless `exec` mode with machine-readable stream-JSON I/O.
Easy Agent similarly separates model communication and orchestration from the
interactive UI. Sonder already owns a bounded, redacted output accumulator,
but its snapshot type exposed implementation-specific enum and ID objects to
callers.

Sources: <https://github.com/gitlawb/zero> and
<https://github.com/ConardLi/easy-agent>

## Implemented slice

`OutputSnapshot.to_dict()` now provides a stable JSON-safe status envelope:

- typed stream IDs and states are rendered as strings;
- counters, digest, preview, truncation, and failure status are retained;
- no raw output or secrets are added to the projection;
- the existing bounded/redacted behavior remains authoritative.

Evidence: `tests/test_output_accumulator.py`.
