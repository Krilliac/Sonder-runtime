# MEM-003 receipt authority slice

Status: `implemented_unverified`.

This slice closes the first-insert and public-value forgery holes in the
receipt observation path. `ReceiptObservationProducer` now requires the
opaque authority attached by the managed verifier boundary and resolves that
authority against the current owner-bound terminal decision before deriving
worker identity, scope, subject digest, and outcome. A copied or modified
`ManagedTerminalEligibility` without that authority is refused, and modified
public fields on a copied value are ignored in favor of the durable resolver.

`SQLiteVerifierObservationRepository` accepts a first insert only when the
producer's in-process authorization capability matches the receipt and
observation. The capability is excluded from serialized receipt data, so
restart/replay still compares the immutable persisted payload. Contradictory
and failed verifier receipts remain negative learning evidence; this slice
does not promote semantic facts or policies.

The authority and insert capability are process-local Python objects. Exact
internal types reject ordinary duck-typed or caller-constructed public values,
but arbitrary code already running in the same Python process can inspect
underscore-prefixed module internals. This is an API and ownership boundary,
not a substitute for process isolation against a malicious extension.

Evidence:

- `tests/test_receipt_observation.py`: 12 focused tests, including public
  eligibility forgery, modified-field substitution, first-insert forgery,
  restart/replay, and immutable conflict behavior.
- `tests/test_managed_terminal_eligibility.py`: 6 focused tests, including the
  live failed-verifier authority attachment.

The full acceptance target remains open until a real managed session produces
the receipt into the production persistence composition and a separately
owned fact-promotion boundary consumes only those records. No semantic fact
claim is accepted from `HostFinalFacts` in this slice.
