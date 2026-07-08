import trilobite_serve as ts


def test_history_from_messages_excludes_last_user_turn():
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow up"},  # current prompt -> excluded
    ]
    hist = ts._history_from_messages(messages)
    assert hist == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]


def test_history_from_messages_drops_system_and_empty():
    messages = [
        {"role": "system", "content": "you are trilobite"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": ""},   # empty -> dropped
        {"role": "user", "content": "current"},
    ]
    hist = ts._history_from_messages(messages)
    assert hist == [{"role": "user", "content": "q1"}]


def test_history_from_messages_single_turn_is_empty():
    hist = ts._history_from_messages([{"role": "user", "content": "only turn"}])
    assert hist == []


def test_history_from_messages_handles_empty():
    assert ts._history_from_messages([]) == []
    assert ts._history_from_messages(None) == []


def test_model_to_tier_defaults_to_local_student():
    for m in ("", None, "trilobite", "gpt-4o-mini"):
        assert ts._model_to_tier(m) is None


def test_model_to_tier_selects_known_tier():
    assert ts._model_to_tier("cloud-code") == "cloud-code"
    assert ts._model_to_tier("general") == "general"


def test_model_to_tier_unknown_falls_back_to_default():
    assert ts._model_to_tier("some-random-model") is None


def test_do_run_returns_structured_error_for_input_program():
    ts.LAST_RESPONSE = "```python\ninput('move: ')\n```"
    out = ts._do_run()

    assert "status: failed" in out
    assert "stderr:" in out
    assert "EOFError" in out
    assert "[exited with error]" in out


def test_run_accepts_optional_timeout():
    ts.LAST_RESPONSE = "```python\nprint('slow smoke')\n```"
    out = ts._handle_slash("/run 12")

    assert "timeout: 12s" in out
    assert "slow smoke" in out


def test_run_rejects_invalid_timeout():
    out = ts._handle_slash("/run python pong.py")

    assert out.startswith("usage: /run [seconds]")
    assert "previous fenced code block" in out
