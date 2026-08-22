from sonder_runtime.domain.prompt_composition import join_system_parts


def test_join_system_parts_keeps_order_and_blank_line_boundaries():
    assert join_system_parts("base", "trace", "persona") == (
        "base\n\ntrace\n\npersona"
    )


def test_join_system_parts_omits_empty_sections_without_extra_separators():
    assert join_system_parts("base", "", None, "tail") == "base\n\ntail"


def test_server_retains_identity_preserving_compatibility_alias():
    import server

    assert server._join_system_parts is join_system_parts
