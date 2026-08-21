# API-005 editor/agent interchange evidence

The existing `editor_interop` application protocol provides a bounded,
provider-neutral interchange contract for editor and agent clients. It treats
`AGENTS.md`, `SKILL.md`, and explicitly allowed rule formats as data: imports
are root-contained and bounded, exports use validated relative paths, and
documents carry deterministic content digests. Versioned envelopes reject
unknown fields and unsupported protocol versions.

Focused evidence:

```text
python -m pytest tests/test_editor_interop.py -q
```

This proves the protocol and filesystem-boundary slice only. A separately
deployed editor transport and live SDK client remain outside this evidence;
the HTTP/MCP surfaces must continue routing imported rules through their
existing instruction and policy boundaries.
