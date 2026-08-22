# MODEL-007 — Controlled escalation

The model gateway now exposes a typed, provider-neutral escalation policy and
application service. A request supplies its permitted route set; the policy
sorts only that set and can move one rank at a time after either a typed high
uncertainty signal or a verifier failure. A successful-looking response never
escalates on its own.

Escalation is fail-closed:

- request budgets are capped by an absolute runtime maximum;
- route count, identifiers, evidence, and provenance are bounded;
- duplicate route IDs or ranks are rejected;
- a trigger without non-empty provenance is denied rather than escalated;
- exhausted budgets and route ladders remain on the current route;
- outcome recording requires the original decision and preserves whether the
  stronger route helped and the evidence provenance that caused the move.

`ControlledEscalationService` is the application boundary. It may emit
low-cardinality decision/outcome events through the existing `EventSink`; it
does not call a provider or invent routes. Provider invocation remains behind
`ModelGateway`, and route availability/consent remains the caller's explicit
request contract.

Verification: `tests/test_remaining_model_007.py` covers uncertainty and
verifier-failure triggers, typed reason/provenance, one-step and absolute
limits, fail-closed missing evidence, route validation, outcome helpfulness,
and event emission. The master checklist and requirement audit are intentionally
unchanged.
