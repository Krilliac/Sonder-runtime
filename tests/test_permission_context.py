from sonder_runtime.domain.permission_context import render_permission_mode_context


def test_render_permission_mode_context_uses_label_and_blurb():
    assert render_permission_mode_context(
        "plan",
        {"plan": "Plan"},
        {"plan": "inspect only"},
        "ask requires a human",
        "elevation: off",
    ) == (
        "permission mode: Plan -- inspect only\n"
        "  ask requires a human\n"
        "elevation: off"
    )


def test_render_permission_mode_context_falls_back_to_unknown_mode():
    assert render_permission_mode_context(
        "future",
        {},
        {},
        "caveat",
        "elevation: off",
    ).startswith("permission mode: future -- \n")


def test_render_permission_mode_context_preserves_multiline_elevation_text():
    out = render_permission_mode_context(
        "auto",
        {"auto": "Auto"},
        {"auto": "automatic"},
        "caveat",
        "elevation: on\n  host process: administrator rights",
    )
    assert out.endswith(
        "elevation: on\n  host process: administrator rights"
    )
