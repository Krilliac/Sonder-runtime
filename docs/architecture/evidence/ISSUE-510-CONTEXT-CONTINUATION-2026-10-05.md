# Original issue 510 context continuation qualification

This record covers original section 1 of
[issue 510](https://github.com/Krilliac/Sonder-runtime/issues/510), rather than the
later A–H strategy research program. Source baseline: `ee428fc2`.

## Concrete behavior

Emergency provider overflow recovery previously removed older turns, including
ordinary prose constraints, accepted decisions and failed attempts. It now
coalesces eligible old text verbatim, preserving each source role. It retries
only when the serialized request shrinks. Older tool protocols, multimodal
content, over-budget text and a projection that would grow the request remain
uncompactable. A refusal leaves the original payload intact and the original
classified failure diagnosable. This reduces message-envelope overhead, not the
words required to preserve reasoning; it cannot fit arbitrary oversized prose.

New durable summaries use schema 3 and retain ordinary conversation text.
Schemas 1 and 2 remain independently re-derivable against their original golden
projections. An authentic older summary that omitted conversation text is
upgraded in the live projection from the exact original source events, without
rewriting its persisted event. Bulky tool output remains a digest-bound pointer.
The lane now serializes nested immutable summary values and resolves these
pointers through its existing `retrieve_archive` tool, after authenticating the
summary and exact source range in the authorized lane session.

## Ordinary functional proof

`tests/test_issue510_context_no_loss.py` covers an untagged offline constraint,
an accepted sqlite decision and its failed JSON rationale, preserved failure
history, conservative refusal, authentic schema-2 recovery, and SQLite lane
restart with a nested failed receipt and retrieval of the complete bulky output.
All four tests fail against the unchanged `ee428fc2` source. They pass with this
change. The focused compaction/context cohort passes 145 tests, including the
unchanged schema-2 golden and the new schema-3 golden. The restart assertion
checks the actual model request and archive tool response, not a fabricated
summary score. Canonical source payloads and persisted old summaries stay intact.
The additional selected lane/server/context cohort passes 362 tests with 10
explicit skips for unavailable systemd process containment. All seven repository
gates pass. The small overflow fixture shrinks from 388 to 378 serialized bytes
while retaining all source text; this is a modest envelope saving, not a claim
of substantive token compression.

## Scope of acceptance

This establishes no silent loss for the tested ordinary conversation and
compaction/restart paths. It does not claim universal live-model quality or
arbitrary-size resumability. Existing bounded history admission may still
require operator-led compaction when protected reasoning exceeds its budget.

The ledger's older child-checkpoint/effect statements must be read together with
[the current effect-journal record](../REMAINING-AGENT-515-EFFECT-JOURNAL.md).
Settled output replay is implemented; unknown effects remain explicitly fenced.
The original twelve-section audit found no further concrete missing primitive
in worker reuse/contracts, ownership/resource admission, deterministic workflow
gates, runtime guards/canaries, learning provenance/demotion, checkpoint state,
hybrid retrieval, or isolated evolution. Their existing tests and platform
qualification are separate evidence, not inferred from this context cohort.

Original section 9 explicitly asks for a pressure scenario without a significant
skill/rule, its recorded failure, the same scenario after adding the skill,
improvement, and regression probes. Held-out publication/promotion contracts
alone do not provide that before/after measurement. That narrowly scoped
qualification remains distinct from later strategy ablations and broader live
routing research. The original cross-cutting promotion criteria likewise apply
to the particular promoted behavior and its measured quality/cost boundary;
a passing suite or bounded fleet breadth alone is not universal task-quality
proof.
