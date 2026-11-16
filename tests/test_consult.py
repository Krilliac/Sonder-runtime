"""Tests for honest N-tier consultation without real model calls.

The existing cases pin an explicit two-tier consult so they do not depend on
whether cloud is enabled in the environment; the N-tier cases exercise the
local+local+cloud default and the "a tier failed but two answered" path.
"""

import consult as consult_module
import server

_LOCAL_PAIR = ["code", "reasoning"]


def test_agreeing_answers_report_high_confidence():
    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "YES. Both answers recommend caching."
        return {
            "code": "Cache the parsed result to avoid repeated work.",
            "reasoning": "Avoid repeated work by caching the parsed result.",
        }[tier]

    result = consult_module.consult(
        "How should this be faster?", _LOCAL_PAIR, ask_fn=ask
    )

    assert result["agree"] is True
    assert result["confidence"] == "high"
    assert [answer["tier"] for answer in result["answers"]] == [
        "code",
        "reasoning",
    ]


def test_disagreeing_answers_report_low_confidence():
    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "NO. They recommend opposite locking strategies."
        return {
            "code": "Hold the lock while calling the callback.",
            "reasoning": "Release the lock before calling the callback.",
        }[tier]

    result = consult_module.consult(
        "Should the callback run under the lock?", _LOCAL_PAIR, ask_fn=ask
    )

    assert result["agree"] is False
    assert result["confidence"] == "low"


def test_one_failed_answer_of_two_reports_unknown_and_names_tier():
    calls = []

    def ask(prompt, tier):
        calls.append((prompt, tier))
        if tier == "reasoning":
            return "ERROR: cloud quota unavailable"
        return "Use a bounded queue."

    result = consult_module.consult(
        "How should work be queued?", _LOCAL_PAIR, ask_fn=ask
    )

    assert result["agree"] is None
    assert result["confidence"] == "unknown"
    assert "reasoning" in result["note"]
    assert len(calls) == 2, "with only one good answer the judge must be skipped"


def test_failed_judge_uses_overlap_heuristic_with_unknown_confidence():
    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "ERROR: judge unavailable"
        return {
            "code": "Use token caching to reduce repeated parsing work.",
            "reasoning": "Token caching reduces repeated parsing work.",
        }[tier]

    result = consult_module.consult(
        "How should parsing be faster?", _LOCAL_PAIR, ask_fn=ask
    )

    assert result["agree"] is True
    assert result["confidence"] == "unknown"
    assert "heuristic" in result["note"].lower()


def _ensemble_reply(text, tier, model, elapsed_ms):
    """``text`` the way ``server.ensemble_answer`` returns one tier's answer."""
    return "%s\n=== ENSEMBLE (1 model answered) ===\n  %s (%s) in %dms" % (
        text, tier, model, elapsed_ms
    )


def test_overlap_verdict_does_not_depend_on_the_ensemble_elapsed_times():
    """The members' "in <N>ms" provenance must not reach the token overlap.

    With the footer counted, these two answers cleared the 0.5 threshold only
    when both members took the same number of milliseconds (10/18 shared
    tokens), and fell below it when they did not (9/19).
    """
    texts = {"code": "It is a failed logon.", "reasoning": "A logon failed."}
    models = {"code": "qwen2.5:3b", "reasoning": "llama3.2:3b"}

    def ask_timed(elapsed):
        def ask(prompt, tier):
            if prompt.startswith("Do these answers agree"):
                return "ERROR: judge unavailable"
            return _ensemble_reply(texts[tier], tier, models[tier], elapsed[tier])

        return ask

    verdicts = set()
    for code_ms, reasoning_ms in [(0, 0), (0, 31), (812, 3), (1500, 1500)]:
        elapsed = {"code": code_ms, "reasoning": reasoning_ms}
        result = consult_module.consult("What is 4625?", _LOCAL_PAIR, ask_fn=ask_timed(elapsed))
        verdicts.add((result["agree"], result["confidence"]))
        # The answers are still shown verbatim, provenance included.
        assert [a["text"] for a in result["answers"]] == [
            _ensemble_reply(texts[t], t, models[t], elapsed[t]) for t in _LOCAL_PAIR
        ]

    # Jaccard of the answers alone: {a, failed, logon} / {it, is, a, failed, logon} = 0.6.
    assert verdicts == {(True, "unknown")}


def test_judge_reply_carrying_the_ensemble_footer_is_still_a_verdict():
    """The judge also answers through the ensemble, so its YES/NO line has a footer."""
    seen = []

    def ask_with(judge_text):
        def ask(prompt, tier):
            if prompt.startswith("Do these answers agree"):
                seen.append(prompt)
                return _ensemble_reply(judge_text, tier, "m-" + tier, 412)
            return _ensemble_reply("Use a bounded queue.", tier, "m-" + tier, 7)

        return ask

    agreed = consult_module.consult("q", _LOCAL_PAIR, ask_fn=ask_with("YES. Same advice."))
    disagreed = consult_module.consult("q", _LOCAL_PAIR, ask_fn=ask_with("NO. They differ."))

    assert (agreed["agree"], agreed["confidence"]) == (True, "high")
    assert (disagreed["agree"], disagreed["confidence"]) == (False, "low")
    # The judge is shown the answers without the run's provenance.
    assert all("ENSEMBLE" not in prompt and "7ms" not in prompt for prompt in seen)
    assert all("ANSWER FROM code:\nUse a bounded queue." in prompt for prompt in seen)


def test_only_a_trailing_ensemble_footer_is_set_aside():
    """Text that merely mentions the footer header is part of the answer."""
    quoted = "Look for this line\n=== ENSEMBLE (1 model answered) ===\nin the server log."
    assert consult_module._substance(quoted) == quoted
    synthesized = (
        "Merged answer.\n=== ENSEMBLE (2 models answered) ===\n"
        "  code (m1) in 5ms\n  reasoning (m2): FAILED - timeout  synthesized by code (m1)"
    )
    assert consult_module._substance(synthesized) == "Merged answer."
    assert consult_module._substance("Plain answer, no footer.") == "Plain answer, no footer."


def test_three_tiers_all_agree_when_cloud_joins():
    seen = []

    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "YES. All three land on the same fix."
        seen.append(tier)
        return "Cache the compiled regex once at import time."

    result = consult_module.consult(
        "How do we avoid recompiling?",
        ["code", "reasoning", "cloud-general"],
        ask_fn=ask,
    )

    assert result["agree"] is True
    assert result["confidence"] == "high"
    assert seen == ["code", "reasoning", "cloud-general"]
    assert "cloud-general" in [a["tier"] for a in result["answers"]]


def test_a_failed_cloud_tier_still_yields_a_verdict_from_two_locals():
    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "YES. Both local answers agree."
        if tier == "cloud-general":
            return "ERROR: hosted/cloud tiers are disabled."
        return "Batch the writes behind one flush."

    result = consult_module.consult(
        "How should writes be batched?",
        ["code", "reasoning", "cloud-general"],
        ask_fn=ask,
    )

    # Two good local answers are enough to judge; the failed cloud tier is noted
    # but does not force the whole consult to "unknown".
    assert result["agree"] is True
    assert result["confidence"] == "high"
    assert "cloud-general" in result["note"]


def test_default_tiers_adds_cloud_only_when_enabled():
    assert consult_module.default_tiers(cloud_ok=False) == ["code", "reasoning"]
    assert consult_module.default_tiers(cloud_ok=True) == [
        "code",
        "reasoning",
        "cloud-general",
    ]


def test_duplicate_tiers_are_collapsed():
    calls = []

    def ask(prompt, tier):
        if prompt.startswith("Do these answers agree"):
            return "YES."
        calls.append(tier)
        return "Same answer."

    consult_module.consult(
        "q", ["code", "code", "reasoning"], ask_fn=ask
    )
    # "code" asked once, not twice: a tier cannot lend independence to itself.
    assert calls == ["code", "reasoning"]


def test_empty_and_case_insensitive_error_answers_are_failures():
    for failed in ("", "error: timeout", "ErRoR: unavailable"):
        result = consult_module.consult("q", _LOCAL_PAIR,
                                        ask_fn=lambda _prompt, _tier: failed)
        assert result["agree"] is None
        assert result["confidence"] == "unknown"


def test_contradictory_judge_is_malformed_and_unicode_overlap_survives():
    calls = {"count": 0}

    def ask(prompt, _tier):
        if prompt.startswith("Do these answers agree"):
            return "YES. They agree. NO. Actually they conflict."
        calls["count"] += 1
        return "使用缓存"

    result = consult_module.consult("q", _LOCAL_PAIR, ask_fn=ask)
    assert result["agree"] is True
    assert result["confidence"] == "unknown"


def test_server_wrapper_uses_active_process_gate_and_current_dispatcher(monkeypatch):
    """Script launch must not consult a second ``server`` module instance.

    The process-local cloud override belongs to the running MCP server.  The
    wrapper therefore passes both that gate and its own dispatcher into the
    otherwise standalone consultation flow.
    """
    seen = []

    def fake_ensemble(prompt, tiers="", **_kwargs):
        seen.append(tiers)
        if prompt.startswith("Do these answers agree"):
            return "YES. The answers agree."
        return "Use a bounded queue."

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "ensemble_answer", fake_ensemble)
    monkeypatch.setattr(
        consult_module,
        "_default_ask",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("wrapper must inject the running server dispatcher")
        ),
    )

    output = server.consult("How should work be queued?")

    assert seen[:3] == ["code", "reasoning", "cloud-general"]
    assert "cloud-general" in output
