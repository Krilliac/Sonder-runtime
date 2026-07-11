import trilobite_repl


def test_piped_utf8_bom_does_not_hide_slash_command():
    assert trilobite_repl._normalize_input_line("\ufeff/inventory .\r\n") == "/inventory ."
    assert trilobite_repl._normalize_input_line("\xef\xbb\xbf/inventory .") == "/inventory ."


def test_normal_repl_input_is_unchanged_except_whitespace():
    assert trilobite_repl._normalize_input_line("  hello trilobite  ") == "hello trilobite"


def test_help_exposes_runtime_policy_and_live_mcp_convergence():
    assert "/runtime" in trilobite_repl.HELP
    assert "/mcp" in trilobite_repl.HELP
    assert "/learning" in trilobite_repl.HELP
