# prompt_clarifier

Detects an underspecified coding prompt from its text alone — no worked
input/output example, no function/method name or signature, or ambiguous
parameter/return types (vague nouns like "data"/"items"/"numbers" with no
concrete type nearby and no Python type hints anywhere) — and returns a list
of specific clarifying questions via `clarify(prompt) -> list[str]` (empty
if the prompt is already well-specified; `is_well_specified(prompt)` is the
boolean convenience). It's valuable because a local inference model can
confidently answer a vague spec anyway, burning a `solver.solve()`
repair loop (or producing code that "looks right" but silently picked the
wrong types/edge cases) instead of asking the one question that would have
fixed it up front. Integration is a pre-filter ahead of generation: in
`server.py`'s `sonder`/`offload` handlers (or before `solver.solve()`),
call `prompt_clarifier.clarify(prompt)` and, if non-empty, return the
questions to the user instead of spending a generation — no changes needed
to solver.py, grounding.py, or verifiers.py, since it only gates whether
generation runs at all. Pure, stdlib-only (just `re`), and fully
deterministic — no GPU, network, or subprocess in the module or its tests.
