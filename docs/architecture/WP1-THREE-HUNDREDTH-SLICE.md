# WP1 Three-Hundredth Slice — agent observation prompt framing

## Boundary

The host-owned framing of tool observations for the agent's model prompt
(`_agent_observation_prompt`, `_frame_agent_observations`,
`_clip_agent_prompt_text`, the untrusted-data envelope header and footer and
the 9000-character default budget) now lives in
`sonder_runtime/domain/agents/observation_prompt.py` as `observation_prompt`,
`frame_observations`, `clip_prompt_text`, `UNTRUSTED_OBSERVATION_HEADER`,
`UNTRUSTED_OBSERVATION_FOOTER` and `OBSERVATION_PROMPT_CHARS`. The two-ended
clip marker, the envelope reservation, the recent-window selection and the
eight-line compaction of older observations are unchanged. `server.py` keeps
every root name as an identity-preserving alias import, so the agent loop,
the autopilot step and finalize prompts, and the ensemble path call the same
objects.

The decision-repair limit and the claim-review regexes that sit beside these
constants deliberately did not move: they belong to the agent loop's
verification policy, a separate responsibility.

## Evidence

- `tests/test_agent_observation_prompt_boundary.py` verifies the six alias identities, two-ended clipping within the limit, the envelope budget, the bounded compaction window with recent evidence retained, and the envelope surviving a tight clip.
- `python -m pytest -q tests/test_agent_observation_prompt_boundary.py tests/test_agent_tools.py tests/test_ensemble_answer.py`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
