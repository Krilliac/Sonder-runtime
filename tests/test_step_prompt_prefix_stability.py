"""Pure observation rendering keeps the previous body as an append prefix."""

from sonder_runtime.domain.agents.observation_prompt import (
    UNTRUSTED_OBSERVATION_FOOTER,
    observation_prompt,
)


def test_step_prompt_prefix_survives_before_the_footer_until_compaction():
    observations = []
    previous = None
    previous_window_chars = 0
    rewrites = []
    transcript = "Task:\nInspect the workspace\n\nHost tools and policy are fixed."
    choice = "\n\nChoose the next tool call or final answer."
    footer = UNTRUSTED_OBSERVATION_FOOTER + choice
    for step in range(1, 100):
        new = "step %d tool=file_read\n%s" % (step, "x" * 400)
        observations.append(new)
        window = observation_prompt(observations, max_chars=6000)
        rendered = transcript + "\n\n" + window + choice
        body = rendered[: -len(footer)]
        if previous is not None:
            if previous_window_chars + len(new) + 2 <= 6000:
                assert body == previous + "\n\n" + new
            else:
                assert not body.startswith(previous)
                rewrites.append(step)
        previous, previous_window_chars = body, len(window)

    assert len(rewrites) >= 3
    assert all(right - left >= 4 for left, right in zip(rewrites, rewrites[1:], strict=False))
