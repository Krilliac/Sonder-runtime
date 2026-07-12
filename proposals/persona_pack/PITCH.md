# persona_pack

A second, specialist-flavored persona set — `debugger`, `security-reviewer`,
`perf-optimizer`, and a code-focused `teacher` — as a drop-in add-on to
`personas.py`'s general set (coder/explainer/reviewer/teacher). Each is a
tightly-scoped system prompt that steers Sonder Runtime's tone and reasoning
discipline for one recurring job (root-cause debugging, exploit-scenario
security review, evidence-before-optimization perf work, line-by-line code
teaching) instead of leaving it to the generic coder persona.

It's valuable because Sonder Runtime's solver/reflection/verifier loop already
does these jobs implicitly; naming the persona lets callers (server.py,
orchestrator.py, a future CLI flag) request the right voice explicitly and
get more consistent, on-task output without touching model weights.

Integration is additive and zero-risk: `personas.py` is never edited.
A caller merges the two registries, e.g.
`{**personas.PERSONAS, **persona_pack.get_pack()}`, or calls
`persona_pack.get(name)` directly wherever a system prompt is currently
pulled from `personas.get(name)`.
