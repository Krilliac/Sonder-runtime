"""Drift tests for command_catalog -- the single source of truth for commands.

The catalog exists because three surfaces disagreed: command_registry listed 59
commands, the REPL answered 151 names, and the MCP server registered 178 tools.
Nothing here asserts a hand-written count; every expectation is re-derived from
the same sources the module derives from, so adding a dispatch branch or an MCP
tool without landing it in the catalog fails a test instead of silently
shrinking /help.
"""
from __future__ import annotations

import os

import pytest

import command_catalog as catalog_module
from command_catalog import (
    CATEGORIES,
    POPULAR,
    by_name,
    catalog,
    categories,
    complete,
    help_text,
    parse_invocation,
)


pytestmark = pytest.mark.unit

_DISPATCH_CHAINS = ("server.py", "sonder_repl.py")


def _registered_tool_names() -> list:
    import server

    return [tool.name for tool in server.mcp._tool_manager.list_tools()]


def _dispatch_slash_names() -> set:
    """Every ``/name`` the REPL and control_command chains actually answer.

    Derived exactly as the catalog derives it -- from the ``cmd == "/x"`` /
    ``cmd in ("/x", "/y")`` comparisons -- so a new branch shows up here the
    moment it is written, whether or not the catalog picked it up.
    """
    names = set()
    for filename in _DISPATCH_CHAINS:
        path = os.path.join(catalog_module._HERE, filename)
        for group in catalog_module._slash_groups(path):
            names.update(group)
    return names


# --- coverage / anti-drift ------------------------------------------------


def test_every_registered_mcp_tool_is_reachable():
    tool_names = _registered_tool_names()
    assert tool_names, "the server registered no tools; the probe is broken"

    fronted = {command.tool for command in catalog() if command.tool}
    unreachable = [
        name for name in tool_names
        if by_name("/" + name) is None and name not in fronted
    ]

    assert not unreachable, (
        "%d of %d registered MCP tools are not reachable from the catalog: %s"
        % (len(unreachable), len(tool_names), ", ".join(sorted(unreachable)))
    )


def test_every_dispatch_chain_slash_name_resolves():
    expected = _dispatch_slash_names()
    assert expected, "no slash names parsed out of the dispatch chains"

    missing = sorted(name for name in expected if by_name(name) is None)

    assert not missing, (
        "%d slash commands answered by %s do not resolve in the catalog: %s"
        % (len(missing), " / ".join(_DISPATCH_CHAINS), ", ".join(missing))
    )


def test_no_two_commands_share_a_canonical_name():
    seen: dict = {}
    duplicates = []
    for command in catalog():
        if command.name in seen:
            duplicates.append(command.name)
        seen[command.name] = command

    assert not duplicates, "duplicate canonical names: %s" % ", ".join(
        sorted(set(duplicates))
    )


def test_no_name_is_claimed_by_two_commands():
    owner: dict = {}
    collisions = []
    for command in catalog():
        for name in command.all_names:
            if name in owner:
                collisions.append("%s claimed by %s and %s" % (
                    name, owner[name], command.name,
                ))
            owner[name] = command.name

    assert not collisions, "; ".join(sorted(collisions))


def test_every_command_category_is_declared():
    unknown = sorted({
        command.category for command in catalog()
        if command.category not in CATEGORIES
    })

    assert not unknown, (
        "categories not in CATEGORIES: %s (known: %s)"
        % (", ".join(unknown), ", ".join(sorted(CATEGORIES)))
    )


def test_categories_view_only_exposes_declared_keys():
    assert set(categories()) <= set(CATEGORIES)
    assert sum(len(v) for v in categories().values()) == len(catalog())


def test_every_popular_entry_resolves():
    missing = [name for name in POPULAR if by_name(name) is None]

    assert not missing, "POPULAR names with no command: %s" % ", ".join(missing)


# --- alias grouping -------------------------------------------------------
#
# REPL delegation lists such as
#   elif cmd in ("/report", "/endreport", "/checklist", "/plan", "/inventory"):
# are NOT alias groups -- reading them as aliases once collapsed 22 unrelated
# commands into one entry and handed /grep the summary belonging to /report.


def test_search_and_grep_are_one_command_about_searching():
    search = by_name("/search")
    grep = by_name("/grep")

    assert search is not None and grep is not None
    assert search is grep, "/search and /grep should be the same command"
    assert "/grep" in search.all_names
    summary = search.summary.lower()
    assert "search" in summary, "/search summary is %r" % search.summary
    assert "report" not in summary, (
        "/search picked up the /report summary: %r" % search.summary
    )


def test_report_is_a_separate_command_from_checklist_and_search():
    report = by_name("/report")
    checklist = by_name("/checklist")
    search = by_name("/search")

    assert report is not None and checklist is not None and search is not None
    assert report.name != checklist.name
    assert report.name != search.name
    assert "/checklist" not in report.all_names
    assert "/search" not in report.all_names
    assert "/grep" not in report.all_names


@pytest.mark.parametrize("spelling", ["/todo", "/task", "/tasks"])
def test_todo_is_canonical_with_task_spellings_as_aliases(spelling):
    command = by_name(spelling)

    assert command is not None
    assert command.name == "/todo", (
        "%s resolved to canonical %s; /todo should be canonical" % (
            spelling, command.name,
        )
    )
    assert set(command.all_names) >= {"/todo", "/task", "/tasks"}


# --- completion -----------------------------------------------------------


def test_bare_and_slash_prefixes_offer_the_popular_set():
    expected = [name for name in POPULAR if by_name(name) is not None]

    for prefix in ("", "/", "  ", " / "):
        offered = [c.name for c in complete(prefix, limit=len(expected))]
        assert offered == expected, "prefix %r offered %s" % (prefix, offered)


def test_adding_characters_never_grows_the_match_set():
    stems = sorted({c.name.lstrip("/") for c in catalog()})
    checked = set()
    grew = []
    for stem in stems:
        for cut in range(1, min(len(stem), 12)):
            shorter, longer = stem[:cut], stem[:cut + 1]
            if (shorter, longer) in checked:
                continue
            checked.add((shorter, longer))
            wide = len(complete("/" + shorter, limit=10 ** 6))
            narrow = len(complete("/" + longer, limit=10 ** 6))
            if narrow > wide:
                grew.append("/%s -> %d but /%s -> %d" % (
                    shorter, wide, longer, narrow,
                ))
    assert checked, "no prefix chains exercised"
    assert not grew, "typing a character grew the match set: %s" % "; ".join(
        sorted(grew)[:10]
    )


def test_git_prefix_returns_only_repo_commands():
    matches = complete("/git", limit=50)

    assert matches, "/git matched nothing"
    off_topic = [c.name for c in matches if c.category != "repo"]
    assert not off_topic, "/git offered non-repo commands: %s" % ", ".join(
        off_topic
    )
    unrelated = [
        c.name for c in matches
        if "git" not in " ".join(c.all_names + (c.summary or "",)).lower()
    ]
    assert not unrelated, "/git offered commands that never mention git: %s" % (
        ", ".join(unrelated)
    )


def test_nonsense_prefix_returns_nothing():
    assert complete("/zzqqxnotacommand") == []
    assert complete("zzqqxnotacommand") == []


@pytest.mark.parametrize("limit", [1, 3, 7])
def test_limit_is_respected(limit):
    assert len(complete("/f", limit=limit)) == limit
    assert len(complete("", limit=limit)) == limit


# --- help -----------------------------------------------------------------


def test_help_overview_lists_every_populated_category():
    overview = help_text("")
    grouped = categories()

    for key in grouped:
        assert key in overview, "category %r missing from /help" % key
        assert CATEGORIES[key] in overview
    assert str(len(catalog())) in overview


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_help_for_a_category_lists_its_commands(category):
    grouped = categories()
    if category not in grouped:
        pytest.skip("category %r currently has no commands" % category)
    text = help_text(category)

    assert text != help_text(""), "/help %s repeated the overview" % category
    assert text.startswith(category)
    for command in grouped[category]:
        assert command.name in text, "%s missing from /help %s" % (
            command.name, category,
        )


def test_help_for_a_command_shows_its_parameters():
    command = by_name("/task_plan")
    assert command is not None and command.params, "/task_plan has no params"
    text = help_text("/task_plan")

    assert "parameters" in text
    for param in command.params:
        assert param.name in text, "%s missing from /help /task_plan" % param.name
    assert "task_plan" in text
    assert text != help_text("")


def test_help_for_an_unknown_command_is_a_message_not_an_exception():
    text = help_text("/nope")

    assert isinstance(text, str) and text.strip()
    assert "nope" in text
    lowered = text.lower()
    assert "no command" in lowered or "did you mean" in lowered
    assert "traceback" not in lowered


# --- parse_invocation -----------------------------------------------------


def test_positional_argument_binds_to_the_first_real_parameter():
    assert parse_invocation("/file_read README.md") == (
        "file_read", {"path": "README.md"},
    )


def test_positional_argument_skips_plumbing_parameters():
    # git_branch declares `root` before `name`; binding positionally to `root`
    # would quietly point the branch command at a directory.
    tool, kwargs = parse_invocation("/git_branch feature-x")

    assert tool == "git_branch"
    assert kwargs == {"name": "feature-x"}
    assert "root" not in kwargs


def test_json_and_quoted_values_survive_parsing():
    tool, kwargs = parse_invocation(
        '/task_plan title="Ship it" steps=["a","b"]'
    )

    assert tool == "task_plan"
    assert kwargs["title"] == "Ship it"
    assert kwargs["steps"] == '["a","b"]'


def test_unknown_key_value_is_rejected_not_dropped():
    with pytest.raises(ValueError) as excinfo:
        parse_invocation("/file_read path=x definitely_not_a_param=5")

    message = str(excinfo.value)
    assert "definitely_not_a_param" in message
    assert "/file_read" in message
    for param in by_name("/file_read").params:
        assert param.name in message


def test_native_command_with_no_backing_tool_returns_none():
    assert by_name("/todo") is not None
    assert by_name("/todo").tool == ""
    assert parse_invocation("/todo") is None
    assert parse_invocation("/todo add something") is None


@pytest.mark.parametrize("line", ["", "   ", "hello world", "file_read x"])
def test_non_slash_line_returns_none(line):
    assert parse_invocation(line) is None
