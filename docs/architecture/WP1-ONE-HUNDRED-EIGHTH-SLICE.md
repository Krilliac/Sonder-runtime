# WP1 One-Hundred-Eighth Slice — lower-layer Ollama security policy

The pure Ollama-origin normalization and fail-closed endpoint policy now live
in `sonder_runtime.domain.ollama_policy`. The transport adapter delegates to
that policy, and `unsafe_lab` uses it directly instead of importing the
adapter.

This is intentionally a policy seam, not a relocation of the stateful
`unsafe_lab` gate or the Ollama transport. It removes the platform-to-adapter
dependency that blocked retiring the `unsafe_lab` root allowance while
preserving the existing public adapter helpers and all security decisions.

Evidence:

- Focused domain, adapter, and unsafe-lab security tests pass.
- Compile, architecture, requirement-evidence, and staged/working diff gates
  pass.
- No transport, unsafe-lab activation state, or security gate was weakened.
