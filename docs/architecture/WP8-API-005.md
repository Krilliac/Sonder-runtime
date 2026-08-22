# WP8 API-005 — Editor/agent interoperability

This slice defines a versioned `ProtocolEnvelope` for editor-to-agent
messages and a bounded import/export boundary for `AGENTS.md`, `SKILL.md`,
and common rule files (`.md`, `.json`, `.yaml`, `.yml`, `.rule`, `.rules`).

All paths are normalized relative paths and are resolved beneath the caller's
root. Traversal and symlink escapes are rejected. Content, payload, and
document-count limits prevent an editor integration from turning the protocol
boundary into an unbounded file or memory ingress. Imported documents are
opaque data; this module does not execute instructions or parse rule semantics.

Focused verification is in `tests/test_wp8_editor_interop.py`.
