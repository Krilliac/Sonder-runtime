# Research port: provider/model profile records

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Zero exposes provider/model profiles and local OpenAI-compatible endpoints as
first-class configuration. Sonder already routes through measured
`CapabilityProfile` values, but those values had no canonical persistence or
identity projection.

Source: <https://github.com/gitlawb/zero>

## Implemented slice

`CapabilityProfile` now supports:

- deterministic JSON-safe `to_dict()` serialization;
- strict `from_dict()` validation of capability names;
- a SHA-256 digest for comparing persisted/measured profile records.

This keeps provider selection in the existing router while making profile
configuration portable across headless, local-model, and future provider
adapters.

Evidence: `tests/test_capability_profiles.py`.
