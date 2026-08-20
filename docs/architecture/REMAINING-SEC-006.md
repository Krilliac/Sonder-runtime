# REMAINING-SEC-006 — Prompt-injection provenance boundary

## Scope

Retrieved memory, web results, and tool results are prompt-visible data but
are not policy, instructions, or trusted memory.  This slice closes the
provenance gap without changing transports or persistence adapters.

## Contract

- Every supported external result is labelled `untrusted` at ingestion.
- The content digest binds the visible bytes to its source kind, source ID,
  origin, and optional parent provenance IDs.
- Promotion to memory or policy requires explicit confirmation and independent
  evidence; no model-visible text can silently promote itself.
- Context packets retain provenance and are deterministic to serialize.
- Replay reconstructs and verifies content digests and the packet digest,
  failing closed on missing, malformed, or tampered provenance.
- The boundary is side-effect free: it does not write memory, alter policy, or
  execute instructions found in retrieved content.

## Evidence

`tests/test_remaining_prompt_provenance.py` covers source labelling,
promotion gating, context/replay preservation, tamper detection, malformed
provenance, and source validation.  Formal specification checkboxes remain
unchanged; this document is implementation evidence only.
