"""The REPL reaches the whole catalogued command surface, not just its branches."""
import builtins

import command_catalog
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


def test_every_tool_typed_as_a_slash_command_dispatches():
    """A tool with no hand-written branch is still reachable from the console."""
    with open(sonder_repl.__file__, encoding="utf-8") as handle:
        source = handle.read()
    # The point of the test is that the catalog carried this, not a branch.
    # If someone later hand-writes /task_progress, pick another unbranched
    # tool rather than deleting the guard.
    assert '"/task_progress"' not in source

    output = sonder_repl._run_catalogued("/task_progress", "/task_progress")

    assert "sonder task progress" in output


def test_positional_argument_binds_to_the_meaningful_parameter():
    tool, kwargs = command_catalog.parse_invocation("/git_branch feature-x")

    assert tool == "git_branch"
    # Not `root`, which git_branch declares first.
    assert kwargs == {"name": "feature-x"}


def test_unknown_parameter_is_reported_rather_than_dropped():
    output = sonder_repl._run_catalogued(
        "/file_read path=README.md limit=5", "/file_read"
    )

    assert "does not take limit" in output
    assert "parameters:" in output


def test_unknown_command_suggests_instead_of_a_bare_refusal():
    output = sonder_repl._run_catalogued("/task_prog", "/task_prog")

    assert "unknown command /task_prog" in output
    assert "/task_progress" in output


def test_a_tool_raising_does_not_escape_the_dispatcher():
    """A failing tool reports; it must not kill the REPL loop."""

    def _boom(**_kwargs):
        raise RuntimeError("exploded")

    original = server.task_progress
    server.task_progress = _boom
    try:
        output = sonder_repl._run_catalogued("/task_progress", "/task_progress")
    finally:
        server.task_progress = original

    assert "failed" in output
    assert "exploded" in output


def test_a_natural_phrase_resolution_still_passes_the_permission_gate():
    """A slash line synthesized from natural language dispatches through the
    same ``_permission_gate`` choke point as a typed slash command: a rule
    deny (or a mode refusal) blocks the tool before its handler ever runs.
    """
    import command_router

    line = command_router.resolve("git status")
    assert line == "/repo_status"

    def _never_runs(**_kwargs):
        raise AssertionError("a refused tool was dispatched anyway")

    original_gate = sonder_repl._permission_gate
    original_tool = server.repo_status
    sonder_repl._permission_gate = (
        lambda tool: (False, "refused /%s: permission rule deny" % tool)
    )
    server.repo_status = _never_runs
    try:
        output = sonder_repl._run_catalogued(line, line.split()[0])
    finally:
        sonder_repl._permission_gate = original_gate
        server.repo_status = original_tool

    assert "refused" in output


def test_help_topics_resolve_to_categories_and_commands():
    overview = command_catalog.help_text("")
    category = command_catalog.help_text("planning")
    command = command_catalog.help_text("/task_plan")

    assert "categories" not in category or category != overview
    assert "planning" in category
    assert "/task_plan" in category
    assert "steps" in command
    assert overview != category != command


def test_read_input_falls_back_to_input_without_a_live_menu(monkeypatch):
    monkeypatch.setattr(sonder_repl, "slash_menu", None)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "typed line")

    assert sonder_repl._read_input("p> ") == "typed line"


def test_a_broken_slash_menu_never_costs_the_user_their_prompt(monkeypatch):
    class _Exploding:
        @staticmethod
        def available():
            raise OSError("no console")

    monkeypatch.setattr(sonder_repl, "slash_menu", _Exploding)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "typed anyway")

    assert sonder_repl._read_input("p> ") == "typed anyway"


def test_framed_composer_preserves_its_prompt_when_raw_input_falls_back(monkeypatch):
    seen = {}

    class _RawFallback:
        @staticmethod
        def available():
            return True

        @staticmethod
        def read_line(_prompt, **kwargs):
            seen.update(kwargs)
            return input(kwargs["fallback_prompt"])

    monkeypatch.setattr(sonder_repl, "slash_menu", _RawFallback)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "typed: " + prompt)

    assert sonder_repl._read_input("sonder > ", composer=True) == "typed: sonder > "
    assert seen["frame"] == "sonder > "


def test_keyboard_interrupt_still_propagates_through_the_menu(monkeypatch):
    class _Interrupting:
        @staticmethod
        def available():
            return True

        @staticmethod
        def read_line(_prompt, **_kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(sonder_repl, "slash_menu", _Interrupting)
    try:
        sonder_repl._read_input("p> ")
    except KeyboardInterrupt:
        return
    raise AssertionError("KeyboardInterrupt must reach the REPL's own handler")


def test_control_command_exposes_help_and_the_bare_slash_gesture():
    assert "categories" in server.control_command("/help").lower() or (
        "commands" in server.control_command("/help").lower()
    )
    assert "planning" in server.control_command("/help planning")
    assert server.control_command("/").startswith("most used")


def test_control_command_leaves_ordinary_prose_alone():
    """A slash line naming no known tool must still reach the model."""
    assert server.control_command("/usr/local/bin is where it lives") is None
