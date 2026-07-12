import sonder_repl


def test_piped_utf8_bom_does_not_hide_slash_command():
    assert sonder_repl._normalize_input_line("\ufeff/inventory .\r\n") == "/inventory ."
    assert sonder_repl._normalize_input_line("\xef\xbb\xbf/inventory .") == "/inventory ."


def test_normal_repl_input_is_unchanged_except_whitespace():
    assert sonder_repl._normalize_input_line("  hello sonder  ") == "hello sonder"


def test_help_exposes_runtime_policy_and_live_mcp_convergence():
    assert "/runtime" in sonder_repl.HELP
    assert "/mcp" in sonder_repl.HELP
    assert "/learning" in sonder_repl.HELP
    assert "/artifactcheck" in sonder_repl.HELP
