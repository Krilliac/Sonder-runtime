# Scoped reviewer context

New reviewer, verifier and adapter-supplied critic requests default to
`WorkerContextPolicy.SCOPED` when their context policy is unspecified.
Explicit `INHERIT`, `CLEAN` or `SCOPED` contracts win. Existing criteria,
commands, file ownership and task scope are preserved. An implicit context-only
default does not require a durable registry; explicit execution requirements do.

Two of six built-in roles change: `verifier` (`build-test`) and `reviewer`.
Explorer, architect, editor and integrator retain their defaults.
`critic` is accepted by the generic worker adapter; it is not a new public
enum member. The solver's existing scoped codegen critic is unchanged.

The workflow retains the original task and latest fenced diff, carries forward
artifact references and recorded verification evidence, and withholds
unstructured implementer/verifier prose. A verifier response without a diff
does not erase the editor's diff before reviewer dispatch. Context digests bind
the task/evidence envelopes; they do not certify a worker's test claims.

## Verifier example

Before:

~~~~text
Continue the agent workflow as the verifier role. Previous role result:
I chose a shortcut because it seems simplest.
```diff
+return 1
```
~~~~

After (SCOPED):

~~~~text
Continue the agent workflow as the verifier role.
Task/spec:
Return one from value().

Diff/artifacts:
+return 1
candidate.patch

Test evidence:
~~~~

## Reviewer example

Before:

~~~~text
Continue the agent workflow as the reviewer role. Previous role result:
I agree with the implementer's shortcut. The tests passed.
~~~~

After (SCOPED):

~~~~text
Continue the agent workflow as the reviewer role.
Task/spec:
Return one from value().

Diff/artifacts:
+return 1
candidate.patch

Test evidence:
pytest: 3 passed
~~~~

Here, test evidence is the explicit evidence supplied to workflow integration,
rather than the verifier's free-form response.

## Adapter critic example

This is a caller-composed task, without a workflow transcript to strip.
For example, this explicit task prompt is identical before and after:

~~~~text
Task/spec: Return one from value().
Diff/artifacts: candidate.patch
Test evidence: pytest: 3 passed
~~~~

The contract changes from UNSPECIFIED to SCOPED, with digest-pinned references
for task/spec, diff/artifacts and test evidence. Caller-supplied task text
remains authoritative; the adapter does not guess which parts of arbitrary
task prose are rationale. The built-in solver critic already receives task,
candidate code and verification evidence without implementer rationale.

An explicit override is supplied through the existing execution contract:

~~~~python
WorkerExecutionContract(
    context_policy=WorkerContextPolicy.INHERIT,
    inherited_context_sha256=parent_context_digest,
)
~~~~

Recursive hypothesis fan-in (`DelegationService.fan_in_hypotheses`) compares
each persisted contract against `effective_execution_contract(...)`, the same
defaulting `DelegationRequest` applies at dispatch, so reviewer, critic and
verifier specialists fan in exactly like other presets.

The worker adapter restores this using the canonical continuation metadata.
No MCP schema, public role enum or committed catalog changes.
