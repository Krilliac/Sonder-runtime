"""A7 observation-window budget and append-only compaction contract."""

from sonder_runtime.domain.agents import observation_prompt as framing


def _without_footer(prompt):
    return prompt[: -len(framing.UNTRUSTED_OBSERVATION_FOOTER)]


def test_old_entries_use_step_tool_first_line_summaries():
    values = [
        "step 1 tool=file_read\nfirst line\nmore file content",
        "step 2 tool=text_search\nsecond result\nmore output",
    ] + ["step %d tool=file_read\n%s" % (n, "x" * 500) for n in range(3, 45)]

    prompt = framing.observation_prompt(values, max_chars=1800)

    assert "Earlier observation summaries" in prompt
    assert "step 1 tool=file_read -> first line" in prompt
    assert len(prompt) <= 1800


def test_compaction_keeps_recent_entries_and_hard_budget():
    values = ["step %d tool=file_read\n%s" % (n, "x" * 400) for n in range(1, 80)]

    prompt = framing.observation_prompt(values, max_chars=20000)

    assert len(prompt) <= 20000
    assert "step 74 tool=file_read" in prompt
    assert "step 79 tool=file_read" in prompt
    assert "Earlier observation summaries" in prompt


def test_overflow_compaction_has_hysteresis_for_steady_observations():
    prompts = []
    values = []
    for step in range(1, 150):
        values.append("step %d tool=file_read\n%s" % (step, "x" * 400))
        prompts.append(framing.observation_prompt(values, max_chars=5000))

    footer = framing.UNTRUSTED_OBSERVATION_FOOTER
    rewrites = []
    for index in range(1, len(prompts)):
        previous = prompts[index - 1]
        current = prompts[index]
        assert current.endswith(footer)
        assert len(current) <= 5000
        if not current[: -len(footer)].startswith(previous[: -len(footer)]):
            rewrites.append(index)

    assert rewrites
    assert all(b - a >= 4 for a, b in zip(rewrites, rewrites[1:], strict=False))


def test_observation_budget_default_is_twenty_thousand():
    assert framing.OBSERVATION_PROMPT_CHARS == 20000


def test_last_six_remain_verbatim_even_above_the_soft_compaction_target():
    values = ["step %d tool=file_read\n%s" % (n, str(n) * 1250) for n in range(10, 19)]
    prompt = framing.observation_prompt(values)
    assert len(prompt) <= 20000
    assert all(value in prompt for value in values[-6:])


def test_six_near_budget_results_take_precedence_over_summary_space():
    values = ["step %d tool=file_read\n%s" % (n, "q" * 3150) for n in range(1, 11)]
    prompt = framing.observation_prompt(values)
    assert len(prompt) <= 20000
    assert all(value in prompt for value in values[-6:])


def test_large_latest_file_is_preserved_and_small_new_results_append():
    values = ["step %d tool=file_read\n%s" % (n, str(n) * 8000) for n in range(1, 5)]
    previous = framing.observation_prompt(values)
    assert values[-1] in previous
    values.append("step 5 tool=text_search\nfound")
    current = framing.observation_prompt(values)
    assert _without_footer(current).startswith(_without_footer(previous))
    assert len(current) <= 20000


def test_tiny_budget_keeps_latest_output_head_and_explicitly_clips():
    values = ["step %d tool=file_read\nMARKER_%d\n%s" % (n, n, "x" * 2500) for n in range(1, 6)]
    prompt = framing.observation_prompt(values, max_chars=1800)
    assert "step 1 tool=file_read" in prompt
    assert "step 5 tool=file_read" in prompt and "MARKER_5" in prompt
    assert "compacted by host" in prompt
    assert len(prompt) <= 1800


def test_budget_parser_handles_missing_invalid_and_small_values():
    assert framing.parse_observation_budget(None) == 20000
    assert framing.parse_observation_budget("oops") == 20000
    assert framing.parse_observation_budget("24000") == 24000
    assert framing.parse_observation_budget("-1") == 512


def test_rendering_is_repeatable_and_does_not_mutate_the_host_ledger():
    values = ["step %d tool=file_read\n%s" % (n, "x" * 400) for n in range(90)]
    saved = tuple(values)
    expected = framing.observation_prompt(values)
    framing.observation_prompt(["unrelated lane " * 20000])
    assert framing.observation_prompt(values) == expected
    assert tuple(values) == saved
