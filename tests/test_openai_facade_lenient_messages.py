"""The chat-completions facade accepts what the chat adapter contract accepts.

OpenAI-compatible clients routinely send an empty system prompt, an empty
prior assistant turn, or ``model: ""``.  The adapter's own validator only
requires a non-empty final user message, drops empty history turns, and
treats a blank model as the default route; the facade must not 400 first.
"""
import pytest

from sonder_runtime.application.protocol.openai_compatibility import (
    CompatibilityError, OpenAICompatibility,
)
from sonder_runtime.interfaces.http import serve


def _chat(messages, model="sonder"):
    return OpenAICompatibility().request(
        {"model": model, "messages": messages}, operation="chat.completions",
    )


def test_empty_system_and_assistant_messages_are_accepted():
    result = _chat([
        {"role": "system", "content": ""},
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "hi"},
    ])
    assert result.messages[-1] == {"role": "user", "content": "hi"}


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "  "}],
    [{"role": "user", "content": "hi"}, {"role": "user", "content": ""}],
    [{"role": "system", "content": "x"}],
])
def test_final_user_message_must_still_be_non_empty(messages):
    with pytest.raises(CompatibilityError):
        _chat(messages)


def test_non_string_content_is_still_rejected():
    with pytest.raises(CompatibilityError, match="string"):
        _chat([{"role": "system", "content": None}, {"role": "user", "content": "hi"}])


@pytest.mark.parametrize("model", ["", "   "])
def test_blank_model_takes_the_default_route(model):
    payload = serve._chat_facade_payload(
        {"model": model, "messages": [{"role": "user", "content": "hi"}]},
        "chat.completions",
    )
    assert payload["model"] == "sonder"


def test_non_string_model_is_left_for_the_facade_to_reject():
    payload = serve._chat_facade_payload(
        {"model": 5, "messages": [{"role": "user", "content": "hi"}]},
        "chat.completions",
    )
    assert payload["model"] == 5
