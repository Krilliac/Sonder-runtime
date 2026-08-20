import server

from sonder_runtime.domain.control_history import control_history_messages


def test_server_keeps_control_history_compatibility_alias():
    assert server._control_history_messages is control_history_messages


def test_control_history_keeps_only_supported_nonempty_turns():
    history = [
        {"role": "system", "content": "hidden"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "ignored"},
        "malformed",
        {"role": "assistant", "content": "answer", "extra": "ignored"},
    ]
    assert control_history_messages(history, "next") == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "next"},
    ]


def test_control_history_handles_missing_history_and_prompt():
    assert control_history_messages(None, "") == []


def test_control_history_preserves_falsy_content_filter_and_prompt_order():
    assert control_history_messages(
        [{"role": "user", "content": 0}, {"role": "assistant", "content": "ok"}],
        "continue",
    ) == [
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "continue"},
    ]
