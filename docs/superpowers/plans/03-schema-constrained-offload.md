# Plan 03 — Schema-constrained offload (new capability)

## Context

Offloading to the local 7B is measured at roughly **53% caller-judged** good
(101 accepted/edited/used against 91 rejected). The operator's own field notes
characterise the failure modes precisely, and they are not random:

- **Transformation works, recall does not.** Given every fact it needs in the
  prompt, output is usable. Asked to supply a fact it was not given (a lookup
  table, an API signature), it invents one confidently — a Win32 ROP2 table came
  back 10 of 16 rows wrong.
- **It writes functions, not systems.** It drops declarations silently, invents
  members, and reaches for a *recalled* API over the one supplied in the prompt.
- Failure looks like *plausible* output, not obviously broken output. A VT
  parser came back with an out-of-bounds read, not a syntax error.

The single highest-leverage intervention for all three is to stop accepting free
text. Ollama supports a `format` parameter taking a JSON Schema, which
constrains decoding so the model *cannot* emit a shape that does not validate.
That converts "plausible prose that must be read carefully" into "a structure
that either parses or is rejected" — and rejection is cheap and automatic.

This is a new capability, not a fix.

## Global Constraints

- **Schema violation must be a hard failure, never a silent coercion.** The
  entire value is that invalid output is rejected. Do not "repair" a bad
  response into a passing one.
- **Do not raise the pass rate by lowering the bar.** Success is measured by the
  caller-judged population in `calibration`, not by how often a call returns
  something.
- Offload must keep working exactly as today when no schema is supplied —
  this is additive.
- Keep private work on local tiers. Nothing in this plan may route a schema or
  payload to a cloud tier that would not already have gone there.
- Every behaviour change needs a test that fails before the change.
- `python scripts/check_error_signals.py` must stay silent (CI ratchet).
- Full suite must stay green: currently **5612 passed, 46 skipped**.

## Task 1 — Plumb a JSON Schema through the offload path

Add an optional `schema` argument to `offload` (and the underlying generate
call) that is passed to Ollama's `format` parameter.

- Validate the returned JSON against the schema *after* generation too — the
  constraint is decoder-side and worth verifying independently rather than
  trusted.
- On violation: return a clear failure naming what did not validate. Do not
  retry silently more than once, and if you retry, say so in the result.
- Record the outcome through `grounded_outcomes`/`record_outcome` so schema
  failures land in the caller-judged population as `rejected` — this is exactly
  the negative signal the store is starved of.

Tests: a valid response parses and returns the object; an invalid one fails with
the offending path named; absence of a schema leaves current behaviour byte-for-
byte unchanged.

## Task 2 — A verified-extraction helper for the recall failure mode

The measured worst case is being asked to *remember* rather than *transform*.
Add a helper that makes that failure detectable instead of invisible:

- Accept the source material and a schema, and require every extracted field to
  be **grounded in the supplied text** — return the supporting span alongside
  each value.
- Reject any field whose claimed span is not actually present in the source.
  That converts confident invention into a mechanical, automatic rejection.

Tests: extraction from real supplied text succeeds with spans; a fabricated
value whose span is absent from the source is rejected; the rejection names the
field.

## Task 3 — Measure whether it actually helped

The point of this plan is a number moving, and a claim of improvement is exactly
the kind this codebase distrusts.

- Add a small, repeatable comparison over a fixed set of prompts run with and
  without a schema, recording outcomes into the existing store.
- Report using `calibration`'s caller-judged population, keeping it separate
  from curriculum results.
- **Do not report an improvement unless both arms reached the same stage.** A
  lower error count because a run aborted earlier is not an improvement; state
  the completion count for each arm alongside the rate.

Tests: the comparison reports per-arm counts; a truncated arm is reported as
such rather than as a better score.
