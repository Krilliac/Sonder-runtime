"""Fanout prompt-echo redaction lives in the domain; the root name is an alias."""
import server
from sonder_runtime.domain import fanout_redaction


def test_root_helper_is_an_identity_preserving_alias():
    assert server._fanout_redact_prompt_echo is fanout_redaction.redact_prompt_echo


def test_a_full_echo_is_replaced_and_short_text_is_untouched():
    prompt = "please summarize the quarterly ledger for the finance team"
    assert fanout_redaction.redact_prompt_echo("A: " + prompt + ".", prompt) == "A: <redacted prompt>."
    assert fanout_redaction.redact_prompt_echo("fine", prompt) == "fine"
    assert fanout_redaction.redact_prompt_echo("anything", "") == "anything"
    assert fanout_redaction.redact_prompt_echo("", prompt) == ""


def test_a_partial_quotation_of_the_prompt_is_redacted_by_span():
    prompt = "please summarize the quarterly ledger for the finance team"
    rendered = "Sure. You asked me to summarize the quarterly ledger for the finance team, here it is."

    # The matched span grows over the shared leading space, as it always did.
    assert fanout_redaction.redact_prompt_echo(rendered, prompt) == (
        "Sure. You asked me to<redacted prompt>, here it is."
    )


def test_a_short_labelled_credential_span_is_redacted():
    prompt = "token=abc12345 rotate it"

    # The span includes the trailing space shared with the prompt.
    assert fanout_redaction.redact_prompt_echo("I saw token=abc12345 in the request", prompt) == (
        "I saw <redacted prompt>in the request"
    )
