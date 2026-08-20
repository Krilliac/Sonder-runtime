from sonder_runtime.domain import thinking_policy


def test_strip_inline_thinking_removes_leading_closed_block():
    assert thinking_policy.strip_inline_thinking(
        "  <thinking>private</thinking>  answer"
    ) == "answer"


def test_strip_inline_thinking_handles_nested_and_repeated_blocks():
    assert thinking_policy.strip_inline_thinking(
        "<think>outer <thinking>inner</thinking></think>"
        "<think>second</think>answer"
    ) == "answer"


def test_strip_inline_thinking_fails_closed_for_unterminated_leading_block():
    assert thinking_policy.strip_inline_thinking("<think>private") == ""


def test_strip_inline_thinking_preserves_nonleading_literal_tags_and_non_strings():
    literal = "Use <think> as an XML example."
    assert thinking_policy.strip_inline_thinking(literal) == literal
    assert thinking_policy.strip_inline_thinking(None) is None


def test_server_keeps_identity_compatible_alias():
    import server

    assert server._strip_inline_thinking is thinking_policy.strip_inline_thinking
