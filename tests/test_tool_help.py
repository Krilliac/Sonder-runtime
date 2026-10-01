"""Focused contracts for the bounded help surface."""
from __future__ import annotations

from sonder_runtime.bootstrap.agent_help_tools import (
    catalog_from_mcp, dispatch_tool_help, discover_agent_tool_registrars,
)
from sonder_runtime.domain.agents.tool_help import advertised_scope, render_tool_help, tool_help_text

SCHEMA = {"type": "object", "properties": {
    "path": {"type": "string"}, "max_items": {"type": "integer"},
    "max_bytes": {"type": "integer"}, "program": {"type": "string"},
    "args": {"type": "array"}}, "required": ["path"]}
CATALOG = {name: {"description": "Use %s for the task." % name,
                  "input_schema": SCHEMA} for name in (
    "directory_tree", "file_find", "text_search", "file_read", "file_read_range",
    "repository_symbol_index", "repo_status", "file_edit", "file_write",
    "text_patch", "file_check", "workspace_run", "test_run", "lint_run",
    "repo_diff", "output_digest")}


FULL = '''Available tools:
- file_read: {"path": "<path>", "max_bytes": 100}
- file_edit: {"path": "<path>", "content": "...", "timeout": 5}
- workspace_run: {"program": "python", "args": ["-m", "pytest"]}
- output_digest: {"path": "<log>", "max_lines": 20}
Reply with exactly one JSON object and no markdown:
{"tool": "tool_name", "args": {...}}
or
{"final": "your final answer"}'''


def test_full_help_is_byte_for_byte_unchanged_without_allowlist():
    assert render_tool_help(FULL, allowlist=None, task_kind="implement") == FULL


def test_implement_help_is_short_and_places_schema_before_footer():
    result = render_tool_help(
        FULL, allowlist={"file_read", "file_edit", "workspace_run"},
        task_kind="implement", catalog=CATALOG,
    )
    assert len(result) <= 2000
    assert result.index("file_edit:") < result.index("Reply with")
    assert "max_" not in result
    assert '"program":"python"' in result
    assert len(result.splitlines()) <= 18


def test_tool_help_name_query_and_unknown_are_bounded():
    one = tool_help_text(name="file_read", catalog=CATALOG)
    assert len(one) <= 1200 and "file_read" in one
    many = tool_help_text(query="read", catalog=CATALOG)
    assert len(many.split(":", 1)[-1].split(",")) <= 5
    unknown = tool_help_text(name="file_reed", catalog=CATALOG)
    assert unknown.count(",") == 2
    assert "closest" in unknown


def test_tool_help_requires_exactly_one_selector():
    assert "exactly one" in tool_help_text(catalog=CATALOG)
    assert "exactly one" in tool_help_text(name="file_read", query="read", catalog=CATALOG)


def test_discovery_returns_sorted_registrars_without_calling_them():
    registrars = discover_agent_tool_registrars()
    assert registrars
    assert [name for name, _ in registrars] == sorted(name for name, _ in registrars)
    assert any(name == "agent_help_tools" for name, _ in registrars)


def test_server_filtered_help_only_advertises_the_run_allowlist():
    import server

    allowed = server._AUTOPILOT_WORKSPACE_TOOLS
    text = server._agent_tool_help(allowlist=allowed, kind="implement")
    advertised = set(server._agent_help_advertised_tools(text))
    assert advertised <= set(allowed)
    assert len(text) <= 2000
    core = ("directory_tree", "file_find", "text_search", "file_read",
            "file_read_range", "repository_symbol_index", "repo_status",
            "file_edit", "file_write", "text_patch", "file_check",
            "workspace_run", "test_run", "lint_run")
    assert all("- %s:" % name in text for name in core)
    # Three workflow lines plus fourteen core entries fit the 18-line core
    # budget; tail/footer lines are allowed after that core.
    assert len(text.splitlines()) <= 22


def test_server_filtered_help_has_no_plumbing_knobs_and_valid_runner():
    import json
    import server

    text = server._agent_tool_help(
        allowlist=server._AUTOPILOT_WORKSPACE_TOOLS, kind="implement"
    )
    footer = next(i for i, line in enumerate(text.splitlines()) if "Reply with exactly one JSON object" in line)
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("-") and ": {" in line:
            assert i < footer
            assert "max_" not in line and "timeout" not in line
    workspace = next(line for line in text.splitlines() if line.startswith("- workspace_run:"))
    payload = json.loads(workspace.split(": ", 1)[1])
    assert payload["program"] in server._AUTOPILOT_RUNNERS


def test_server_full_help_path_remains_available_without_filter():
    import server

    full = server._agent_tool_help()
    assert "- file_read:" in full
    assert "Reply with exactly one JSON object" in full


def test_discovered_help_registrar_can_register_on_a_minimal_mcp():
    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(fn):
                self.tools[fn.__name__] = fn
                return fn
            return decorate

    mcp = FakeMcp()
    registrars = dict(discover_agent_tool_registrars())
    registrars["agent_help_tools"](mcp, lambda *args, **kwargs: None)
    assert "tool_help" in mcp.tools
    assert len(mcp.tools["tool_help"](query="read").split(",")) <= 5


def test_live_parameters_catalog_keeps_required_knob_and_filters_optional_knobs():
    class Tool:
        description = "Check a file for syntax and lint issues."
        parameters = {"type": "object", "properties": {
            "path": {"type": "string"}, "max_items": {"type": "integer"},
            "timeout_seconds": {"type": "number"}},
            "required": ["path", "max_items"]}

    class M:
        class Manager:
            _tools = {"file_check": Tool()}
        _tool_manager = Manager()

    catalog = catalog_from_mcp(M())
    assert "max_items" in tool_help_text(name="file_check", catalog=catalog)
    assert "timeout_seconds" not in tool_help_text(name="file_check", catalog=catalog)


def test_catalog_only_generated_schema_precedes_footer_and_keeps_required_knob():
    lines = "- output_digest: input_schema={\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"},\"max_items\":{\"type\":\"integer\"},\"timeout_seconds\":{\"type\":\"number\"}},\"required\":[\"path\",\"max_items\"]}\nReply with exactly one JSON object"
    result = render_tool_help(lines, allowlist={"output_digest"}, task_kind="report", catalog={"output_digest": {"input_schema": SCHEMA}})
    assert result.index("output_digest:") < result.index("Reply with")
    assert "max_items" in result
    assert "timeout_seconds" not in result
    assert len(result) <= 2000


def test_scope_and_dispatch_never_leak_denied_names_or_synonyms():
    class Tool:
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}}
        description = "Read a file."

    class M:
        class Manager:
            _tools = {"file_read": Tool(), "secret_admin": Tool()}
        _tool_manager = Manager()

    with advertised_scope({"file_read"}):
        result = dispatch_tool_help(M(), {"query": "admin"})
        unknown = dispatch_tool_help(M(), {"name": "secret_admin"}, advertised={"file_read"})
    assert "secret_admin" not in result
    assert "secret_admin" not in unknown


def test_map_fixture_step_one_fits_prompt_budget(monkeypatch, tmp_path):
    """Match map/measure_prompt's first user message without invoking a model."""
    import server
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return '{"final":"done"}'

    generate.last_response_meta = {}
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: generate)
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: ("fixture-model", False, False, "code"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    run = {"project": str(tmp_path), "policy": "workspace", "allow_web": False}
    worker = server._prompts.render(
        "autopilot_worker", objective="Fix the bug in pkg/util.py", task_id="task-01",
        kind="implement", title="Fix bug",
        instruction="Edit pkg/util.py so func_1 returns a - b * 1 and run the tests.",
        criteria="- tests pass", prior="(none yet)")
    server._agent_turn(worker, tier="code", max_steps=1, allow_web=False,
                       auto_checklist=True, project=str(tmp_path),
                       tool_allowlist=server._autopilot_allowed_tools(run),
                       tool_help_kind="implement", return_host_receipt=True)
    assert prompts
    print("A8 step-1 user message chars:", len(prompts[0]))
    assert len(prompts[0]) <= 5000
    assert "HOST TOOL ALLOWLIST" not in prompts[0]


def test_server_full_help_matches_legacy_unfiltered_projection():
    import server
    original = server.AGENT_TOOL_HELP
    generated = server._generic_agent_dispatch.generated_help_lines(
        server.mcp, server._agent_help_advertised_tools(original), server._AGENT_SYSTEM_OPERATOR_TOOLS)
    if generated:
        original = original.rstrip() + "\n" + "\n".join(generated) + "\n"
    denied = {name for name in server._agent_help_advertised_tools(original)
              if server._agent_run_tool_refusal(name)}
    expected = original if not denied else "\n".join(
        line for line in original.splitlines()
        if not any(line.lstrip().startswith("- %s:" % name) for name in denied))
    assert server._agent_tool_help() == expected


def test_filtered_tail_is_part_of_the_allowlist_promise():
    import server
    allowed = server._autopilot_allowed_tools({"policy": "workspace", "project": ""})
    text = server._agent_tool_help(allowlist=allowed, kind="implement", allow_web=False)
    tail = next(line for line in text.splitlines() if line.startswith("other tools"))
    names = set(tail.split("): ", 1)[1].split(", "))
    assert names <= allowed
    assert "web_fetch" not in names and "web_search" not in names
