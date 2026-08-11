"""Drift guards for the advertising surfaces outside the agent help blocks.

``tests/test_agent_help_dispatch_drift.py`` covers the agent *help* blocks.
Three further surfaces name tools or actions to a model or an operator, and
none of them was checked against the thing that actually runs:

* the autopilot host allowlists (``_AUTOPILOT_OBSERVE_TOOLS`` /
  ``_AUTOPILOT_WORKSPACE_TOOLS``).  These are not merely policy sets -- both
  ``_agent_impl`` ("HOST TOOL ALLOWLIST (cannot be expanded by the model)")
  and ``_autopilot_plan_model`` ("Allowed tools: ...") render them verbatim
  into the model transcript, so every name in them is a promise;
* ``tool_manifest()``, whose slash-separated keys are read as tool names by
  both models and operators;
* the ``loop`` action vocabulary -- the ``"Valid action types: ..."`` string
  an unknown action returns, and the ``loop`` docstring MCP clients show.

Every assertion here recomputes both sides from source: registration from the
live MCP tool manager, dispatchability from ``_agent_dispatch``, and the loop
vocabulary from ``_loop_dispatch``'s own branch table.  Each parser is
separately proved non-vacuous, because a parser that silently stopped matching
would turn every subset assertion below into a tautology over the empty set --
which is exactly how a validator here once reported ``ok`` while covering 15
of 184 tools.
"""
from __future__ import annotations

import ast
import inspect
import itertools
import re

import server
import tool_capabilities as capabilities


# Floors, not expected values: they exist so an empty extractor fails loudly
# instead of satisfying every subset assertion below.
_MIN_REGISTERED_TOOLS = 150
_MIN_DISPATCH_BRANCHES = 90
_MIN_LOOP_BRANCHES = 50
_MIN_MANIFEST_NAMES = 100


def _registered_tools():
    """Names the MCP server actually registered, from the live tool manager."""
    return frozenset(server.mcp._tool_manager._tools)


def _help_advertised(help_text):
    names = set()
    for line in help_text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        name, separator, _ = stripped[2:].partition(":")
        name = name.strip()
        if separator and name.isidentifier():
            names.add(name)
    return frozenset(names)


def _manifest_advertised(manifest_text):
    """Tool names ``tool_manifest`` advertises, one per slash-separated key."""
    names = set()
    for line in manifest_text.splitlines():
        key, separator, _ = line.strip().partition(":")
        if not separator:
            continue
        names.update(
            part.strip() for part in key.split("/")
            if part.strip() and part.strip().isidentifier()
        )
    return frozenset(names)


def _agent_help_texts():
    """Every agent help surface, discovered rather than listed."""
    texts = {
        name: value for name, value in vars(server).items()
        if name.endswith("_TOOL_HELP") and isinstance(value, str)
    }
    flags = tuple(
        name
        for name, parameter in inspect.signature(
            server._agent_tool_help
        ).parameters.items()
        if isinstance(parameter.default, bool)
    )
    for combination in itertools.product((False, True), repeat=len(flags)):
        keywords = dict(zip(flags, combination))
        label = "_agent_tool_help(%s)" % ", ".join(
            "%s=%s" % item for item in sorted(keywords.items())
        )
        texts[label] = server._agent_tool_help(**keywords)
    return texts


def _advertising_surfaces():
    """Label -> set of tool names that surface promises a model or operator."""
    surfaces = {
        label: _help_advertised(text)
        for label, text in _agent_help_texts().items()
    }
    surfaces["REPOSITORY_READ_ONLY_TOOLS"] = frozenset(
        server.REPOSITORY_READ_ONLY_TOOLS
    )
    surfaces["_AUTOPILOT_OBSERVE_TOOLS"] = frozenset(
        server._AUTOPILOT_OBSERVE_TOOLS
    )
    surfaces["_AUTOPILOT_WORKSPACE_TOOLS"] = frozenset(
        server._AUTOPILOT_WORKSPACE_TOOLS
    )
    surfaces["tool_manifest()"] = _manifest_advertised(server.tool_manifest())
    return surfaces


def _loop_action_branches():
    """Alias groups ``_loop_dispatch`` actually implements, one tuple/branch."""
    tree = ast.parse(inspect.getsource(server._loop_dispatch))
    branches = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "action_type":
            continue
        comparator = node.comparators[0]
        if isinstance(node.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
            if isinstance(comparator.value, str):
                branches.append((comparator.value,))
        elif isinstance(node.ops[0], ast.In) and isinstance(
            comparator, (ast.Set, ast.Tuple, ast.List)
        ):
            branches.append(tuple(
                item.value for item in comparator.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ))
    return tuple(branch for branch in branches if branch)


def _loop_error_advertised():
    """Action names the unknown-action reply lists back to the caller."""
    result = server._loop_dispatch({"type": "__drift_probe_unknown__"})
    assert result["ok"] is False
    marker = "Valid action types:"
    body = result["output"]
    assert marker in body
    listed = body[body.index(marker) + len(marker):].strip().rstrip(".")
    return frozenset(part.strip() for part in listed.split(",") if part.strip())


def _loop_docstring_advertised():
    """Action names the ``loop`` docstring's full-vocabulary line names."""
    doc = server.loop.__doc__ or ""
    marker = "All valid `type` values:"
    assert marker in doc, "loop docstring no longer states its full vocabulary"
    tail = doc[doc.index(marker) + len(marker):]
    listed = tail.split(".", 1)[0]
    return frozenset(part.strip() for part in listed.split(",") if part.strip())


# --------------------------------------------------------------------------
# Non-vacuity: every extractor above must be proved to still see things.
# --------------------------------------------------------------------------

def test_extractors_cannot_go_vacuous():
    registered = _registered_tools()
    assert len(registered) >= _MIN_REGISTERED_TOOLS
    assert "memory_search" in registered
    # The AST view of registration must agree with the live manager, or one of
    # the two is measuring something other than "registered MCP tool".
    module = ast.parse(inspect.getsource(server))
    decorated = set()
    for node in module.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                decorated.add(node.name)
    assert decorated == set(registered)

    assert len(capabilities.dispatch_names(server._agent_dispatch)) >= _MIN_DISPATCH_BRANCHES

    surfaces = _advertising_surfaces()
    assert {
        "AGENT_TOOL_HELP",
        "REPOSITORY_AGENT_TOOL_HELP",
        "_AUTOPILOT_OBSERVE_TOOLS",
        "_AUTOPILOT_WORKSPACE_TOOLS",
        "tool_manifest()",
    } <= set(surfaces)
    for label, names in sorted(surfaces.items()):
        assert len(names) >= 30, label
    assert len(surfaces["tool_manifest()"]) >= _MIN_MANIFEST_NAMES
    # The manifest parser must still see a newly added key.
    probed = _manifest_advertised(
        server.tool_manifest() + "\n  __drift_probe__/__drift_probe_two__: x"
    )
    assert {"__drift_probe__", "__drift_probe_two__"} <= probed

    branches = _loop_action_branches()
    assert len(branches) >= _MIN_LOOP_BRANCHES
    flattened = {name for branch in branches for name in branch}
    assert {"code", "sleep", "memory_search"} <= flattened
    assert len(_loop_error_advertised()) >= _MIN_LOOP_BRANCHES
    assert len(_loop_docstring_advertised()) >= _MIN_LOOP_BRANCHES


# --------------------------------------------------------------------------
# #22 shape: advertised but never registered.
# --------------------------------------------------------------------------

def test_no_surface_advertises_an_unregistered_tool():
    registered = _registered_tools()
    aliases = frozenset(server._AGENT_TOOL_ALIASES)
    for label, names in sorted(_advertising_surfaces().items()):
        unregistered = sorted(names - registered - aliases)
        assert unregistered == [], (
            "%s advertises names that are not registered MCP tools: %s"
            % (label, unregistered)
        )


def test_agent_tool_aliases_all_resolve_to_registered_tools():
    """The alias allowance above must not be able to launder a fake name."""
    registered = _registered_tools()
    unresolved = sorted(
        "%s -> %s" % item
        for item in server._AGENT_TOOL_ALIASES.items()
        if item[1] not in registered
    )
    assert unresolved == []


# --------------------------------------------------------------------------
# #16 shape: autopilot advertises what dispatch cannot run.
# --------------------------------------------------------------------------

def test_autopilot_allowlists_only_name_dispatchable_tools():
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    for label in ("_AUTOPILOT_OBSERVE_TOOLS", "_AUTOPILOT_WORKSPACE_TOOLS"):
        allowlist = frozenset(getattr(server, label))
        gap = sorted(allowlist - dispatch)
        assert gap == [], (
            "%s is rendered verbatim into the model transcript but %d of its "
            "names have no _agent_dispatch branch: %s" % (label, len(gap), gap)
        )


def test_autopilot_observe_allowlist_survives_repository_read_only_policy():
    """Observe runs are read_only, so the allowlist must clear that gate too."""
    for name in sorted(server._AUTOPILOT_OBSERVE_TOOLS):
        assert name in server.REPOSITORY_READ_ONLY_TOOLS, (
            "%s is advertised to an observe-policy autopilot run, which "
            "_agent_impl runs read_only, but repository policy denies it"
            % name
        )


# --------------------------------------------------------------------------
# #32 shape: loop advertises fewer actions than it implements.
# --------------------------------------------------------------------------

def test_loop_error_message_is_rendered_from_the_action_vocabulary():
    assert _loop_error_advertised() == frozenset(server._LOOP_ACTION_TYPES)
    assert len(server._LOOP_ACTION_TYPES) == len(set(server._LOOP_ACTION_TYPES))


def test_loop_advertises_every_action_type_it_implements():
    advertised = _loop_error_advertised()
    unadvertised = sorted(
        "|".join(branch) for branch in _loop_action_branches()
        if not advertised.intersection(branch)
    )
    assert unadvertised == [], (
        "loop implements action types no advertising surface names "
        "(capability hidden from every caller): %s" % unadvertised
    )


def test_loop_advertises_no_action_type_it_does_not_implement():
    implemented = {name for branch in _loop_action_branches() for name in branch}
    for label, advertised in (
        ("unknown-action reply", _loop_error_advertised()),
        ("loop docstring", _loop_docstring_advertised()),
    ):
        phantom = sorted(advertised - implemented)
        assert phantom == [], "%s names unimplemented actions: %s" % (
            label, phantom,
        )


def test_loop_docstring_and_error_reply_advertise_the_same_vocabulary():
    assert _loop_docstring_advertised() == _loop_error_advertised()


def test_loop_docstring_examples_are_all_real_action_types():
    implemented = {name for branch in _loop_action_branches() for name in branch}
    examples = set(re.findall(
        r'\{"type"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"', server.loop.__doc__ or "",
    ))
    assert len(examples) >= 20
    assert sorted(examples - implemented) == []
