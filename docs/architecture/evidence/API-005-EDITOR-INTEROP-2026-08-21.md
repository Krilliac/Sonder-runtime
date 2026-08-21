# API-005 editor/agent interchange evidence

The existing `editor_interop` application protocol provides a bounded,
provider-neutral interchange contract for editor and agent clients. It treats
`AGENTS.md`, `SKILL.md`, and explicitly allowed rule formats as data: imports
are root-contained and bounded, exports use validated relative paths, and
documents carry deterministic content digests. Versioned envelopes reject
unknown fields and unsupported protocol versions.

The ACP-oriented extension also validates bounded peer implementation metadata
and represents per-request cancellation as a versioned envelope. The thin
`EditorStdioTransport` now carries those envelopes over bounded newline-
delimited streams, requires one-time initialization, correlates responses,
delivers cancellation through an application callback, and redacts handler
exception details. Filesystem and tool operations remain application-owned.

Focused evidence:

```text
python -m pytest tests/test_editor_interop.py tests/test_editor_transport.py -q
```

This proves the bounded envelope, filesystem boundary, local stdio framing,
initialization, cancellation, correlation, and error-containment slices. A
separately deployed editor transport and live SDK client remain outside this
evidence; the HTTP/MCP surfaces must continue routing imported rules through
their existing instruction and policy boundaries. Full ACP capability
negotiation, elicitation, and cross-implementation interoperability remain
adapter-level work.
