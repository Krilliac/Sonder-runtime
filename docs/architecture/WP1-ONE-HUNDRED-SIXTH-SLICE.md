# WP1 One-Hundred-Sixth Slice: system-profile ownership

## Boundary

Full `system_profile` implementation ownership now lives in
`sonder_runtime.platform.system_profile`. The root `system_profile.py` remains
an identity-preserving compatibility shim for the server and existing tools.

Adaptive training, self-heal, bootstrap, and NPU callers use the packaged
module directly. The canonical module preserves mutable probe state, hardware
detection, environment overrides, profile editing, and legacy monkeypatch
identity.

## Evidence

- Ownership and compatibility tests cover module identity, legacy monkeypatch
  behavior, profile editing, and RAM/NPU overrides.
- Existing system-profile, adaptive-training, self-heal, and NPU profile tests
  remain the regression surface.
- Architecture, compile, requirement-evidence, and diff gates are required.

No server, persistence, command-catalog, launcher, HTTP/REPL, or strangler
services files are part of this slice.
