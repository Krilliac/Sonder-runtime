# WP1 root-server boundary evidence

## Decision

No production slice was moved in this checkpoint. The existing typed vision
port is not a behaviorally complete replacement for the legacy root's
`vision_analyze` path: the root implementation combines token/approval
authorization, legacy model-catalog and endpoint policy, native-context
policy, guarded file inspection, activity/direct-tool recording, and legacy
error rendering. The typed `VisionRequest`/`VisionGateway` contract does not
carry those HTTP/REPL-facing responsibilities.

Moving only the inference call would therefore risk changing request refusal
ordering, error text, telemetry, or the command surfaces. Session/context,
OpenAI, and MCP seams are explicitly out of scope for closing that boundary.

## Remaining dependency graph

`sonder_runtime.bootstrap.legacy_root` is the sole packaged module that
imports the historical `server` root. The explicit bootstrap seams
`legacy_interfaces`, `legacy_model`, and `legacy_mcp` are the only packaged
callers of `legacy_root`; HTTP and REPL receive the runtime through their
existing configured proxy. No other packaged production module may import
`server`.

## Narrow-slice audit

The remaining callers were re-audited against the requested root-free slice:

| Caller | Ownership | Why it is not safe in this slice |
| --- | --- | --- |
| `bootstrap/legacy_interfaces.py` | HTTP/REPL composition | Moving it can change HTTP or REPL runtime injection and behavior. |
| `bootstrap/legacy_model.py` | model bootstrap | Model bootstrap is explicitly excluded from this slice. |
| `bootstrap/legacy_mcp.py` | MCP bootstrap | MCP is explicitly excluded from this slice. |

There is therefore no remaining caller/adapter that can be moved safely while
honoring the requested disjoint write set. The boundary ratchet classifies
every remaining caller as one of these three excluded seams and fails if any
additional caller appears. No production migration was applied in this audit.

## Ratchet

`tests/test_wp1_root_server_boundary.py` proves both sides of this boundary:

- a new package-internal `server` import fails the test;
- `legacy_root` still owns the one intentional import; and
- the three bootstrap composition modules are the complete caller set; and
- every remaining caller is explicitly classified as an excluded HTTP/REPL,
  model-bootstrap, or MCP seam.

This keeps WP1 shrink-only while preserving the current HTTP/REPL semantics
until a typed port can represent the full vision boundary.
