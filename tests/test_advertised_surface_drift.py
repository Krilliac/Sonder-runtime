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

Advertised-vs-dispatchable is not sufficient on its own.  A tool can be
advertised on a surface, admitted by that surface's policy, genuinely
dispatchable -- and still refused a step later by a *second* gate, so the model
is told it may call the tool, calls it, and is refused.  A dispatch-only check
cannot see that shape, because the tool does dispatch.

``_agent_impl`` has three such gates that no surface and no guard measured:

* ``project_scope`` + ``_PROJECT_BOUND_AGENT_TOOLS`` (server.py, "has no
  project-bound execution contract");
* ``allow_web``, checked *inside* ``_agent_dispatch``'s own branch bodies;
* ``allow_location``, likewise.

``_agent_tool_help`` filtered on ``read_only``/``cloud``/``unsafe`` only, so
each of these produced dead vocabulary.  The section at the bottom of this file
asserts that no advertising surface names a tool a run gate will
unconditionally refuse, for every combination of the run flags.
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


def test_agent_tool_alias_keys_and_targets_are_both_real():
    """Close the laundering route the allowance above opens.

    The allowance subtracts alias **keys**, so until this asserted anything
    about keys, writing ``_AGENT_TOOL_ALIASES["__ghost__"] = "memory_search"``
    and advertising ``__ghost__`` on ``tool_manifest()`` was invisible to
    every guard in the repository -- the exact #22 defect, on the same
    surface, reached through the guard's own exemption.  ``_agent_dispatch``
    does not resolve aliases (``_AGENT_TOOL_ALIASES`` appears nowhere in its
    source; resolution happens separately in ``_canonical_agent_tool_name``),
    so requiring each key to have its own dispatch branch is what makes the
    exemption safe.  Targets are checked too, for the other direction.
    """
    registered = _registered_tools()
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    assert len(server._AGENT_TOOL_ALIASES) >= 5
    undispatchable_keys = sorted(set(server._AGENT_TOOL_ALIASES) - dispatch)
    assert undispatchable_keys == [], (
        "alias keys with no _agent_dispatch branch are exempted from the "
        "registration check above while being unreachable: %s"
        % undispatchable_keys
    )
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


# --------------------------------------------------------------------------
# admit-then-deny: a surface advertises it, a policy admits it, a SECOND gate
# refuses it one step later.  Invisible to every check above, because the tool
# genuinely dispatches.
# --------------------------------------------------------------------------

def _flag_gated_tools(flag):
    """Tools whose ``_agent_dispatch`` branch returns early on ``not <flag>``.

    These are name-unconditional refusals living *inside* the dispatcher, so
    ``dispatch_names`` counts them as dispatchable and no advertised-vs-
    dispatchable check can see them.
    """
    tree = ast.parse(inspect.getsource(server._agent_dispatch))
    gated = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "tool_name"
            and len(test.ops) == 1
        ):
            continue
        comparator = test.comparators[0]
        if isinstance(test.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
            names = (comparator.value,)
        elif isinstance(test.ops[0], ast.In) and isinstance(
            comparator, (ast.Set, ast.Tuple, ast.List)
        ):
            names = tuple(
                item.value for item in comparator.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
        else:
            continue
        for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if (
                isinstance(inner, ast.If)
                and isinstance(inner.test, ast.UnaryOp)
                and isinstance(inner.test.op, ast.Not)
                and isinstance(inner.test.operand, ast.Name)
                and inner.test.operand.id == flag
                and any(isinstance(stmt, ast.Return) for stmt in inner.body)
            ):
                gated.update(name for name in names if isinstance(name, str))
    return frozenset(gated)


def _agent_impl_call_keywords(function):
    """Constant keyword arguments a caller pins on its ``_agent_impl`` call."""
    tree = ast.parse(inspect.getsource(function))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = target.attr if isinstance(target, ast.Attribute) else getattr(
            target, "id", "",
        )
        if name != "_agent_impl":
            continue
        return {
            keyword.arg: keyword.value.value
            for keyword in node.keywords
            if keyword.arg and isinstance(keyword.value, ast.Constant)
        }
    return {}


def test_flag_gate_extractors_cannot_go_vacuous():
    web = _flag_gated_tools("allow_web")
    location = _flag_gated_tools("allow_location")
    assert "web_search" in web and "web_fetch" in web
    assert location, "allow_location gate extractor sees nothing"
    assert location <= web
    # The declared constants must match what the dispatcher actually does, or
    # the filter below is protecting against the wrong set.
    assert web == frozenset(server._AGENT_WEB_GATED_TOOLS)
    assert location == frozenset(server._AGENT_LOCATION_GATED_TOOLS)
    # And the project-bound gate must still be a real, narrowing gate: it has
    # to refuse dispatchable tools, or every assertion below is a tautology.
    # (Note it is deliberately NOT asserted to be a subset of the dispatch
    # branches -- 24 of its names have no branch at all.  That is inert rather
    # than harmful, because it is a permit set, not an advertising surface.)
    assert len(server._PROJECT_BOUND_AGENT_TOOLS) >= 30
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    refused = dispatch - frozenset(server._PROJECT_BOUND_AGENT_TOOLS)
    assert len(refused) >= 10, refused


def _run_flag_combinations():
    """Every run-flag combination ``_agent_tool_help`` accepts."""
    flags = tuple(
        name
        for name, parameter in inspect.signature(
            server._agent_tool_help
        ).parameters.items()
        if isinstance(parameter.default, bool)
    )
    for combination in itertools.product((False, True), repeat=len(flags)):
        yield dict(zip(flags, combination))


def test_agent_help_advertises_nothing_a_run_gate_will_refuse():
    """The admit-then-deny guard.

    For every combination of run flags, no name the help text advertises may
    be one that ``_agent_run_tool_refusal`` refuses for that same
    combination.  Advertising it means the model is told it can call the tool
    and is refused one step later -- and the run pays a step for it.
    """
    for keywords in _run_flag_combinations():
        help_text = server._agent_tool_help(**keywords)
        advertised = _help_advertised(help_text)
        assert advertised, "help went empty for %s" % keywords
        refused = sorted(
            "%s (%s)" % (name, gate)
            for name, gate in (
                (name, server._agent_run_tool_refusal(name, **keywords))
                for name in advertised
            )
            if gate
        )
        assert refused == [], (
            "_agent_tool_help(%s) advertises %d tool(s) that a later gate "
            "unconditionally refuses on exactly that run: %s"
            % (
                ", ".join("%s=%s" % item for item in sorted(keywords.items())),
                len(refused),
                refused,
            )
        )


def test_project_bound_help_still_advertises_a_usable_surface():
    """The filter must narrow the surface, not empty it."""
    unbound = _help_advertised(server._agent_tool_help())
    bound = _help_advertised(server._agent_tool_help(project_bound=True))
    assert bound < unbound, "project-bound filter removed nothing"
    assert len(bound) >= 30, bound
    assert "file_read" in bound


def test_autopilot_workspace_allowlist_survives_the_project_bound_gate(tmp_path):
    """F1: the literal sibling of the dispatch bug this file already guards.

    ``_AUTOPILOT_WORKSPACE_TOOLS`` is rendered verbatim into the transcript as
    "HOST TOOL ALLOWLIST (cannot be expanded by the model)".  On a project-
    bound run every name outside ``_PROJECT_BOUND_AGENT_TOOLS`` is refused.
    """
    project = str(tmp_path)
    # The scope must actually resolve, or this test binds nothing.
    assert server._agent_project_scope(project)[0], project
    for policy in ("workspace", "observe"):
        run = {"policy": policy, "project": project}
        allowed = server._autopilot_allowed_tools(run)
        assert allowed, run
        gap = sorted(frozenset(allowed) - frozenset(server._PROJECT_BOUND_AGENT_TOOLS))
        assert gap == [], (
            "the %s allowlist a project-bound autopilot run renders into its "
            "transcript names %d tool(s) with no project-bound execution "
            "contract: %s" % (policy, len(gap), gap)
        )
    # An unbound run must keep its full allowlist -- the narrowing is scoped.
    unbound = server._autopilot_allowed_tools({"policy": "workspace"})
    assert frozenset(unbound) == frozenset(server._AUTOPILOT_WORKSPACE_TOOLS)


def test_orchestrator_worker_help_names_no_tool_its_own_flags_refuse():
    """F4: ``_orchestrator_agent_worker`` pins ``allow_web=False``.

    The flags are read out of the call itself rather than restated here, so
    changing the call changes what this test checks.
    """
    keywords = _agent_impl_call_keywords(server._orchestrator_agent_worker)
    assert keywords, "no _agent_impl call found in _orchestrator_agent_worker"
    assert keywords.get("allow_web") is False, keywords
    help_text = server._agent_tool_help(
        read_only=bool(keywords.get("read_only")),
        project_bound=True,
        allow_web=bool(keywords.get("allow_web")),
        allow_location=bool(keywords.get("allow_location")),
    )
    advertised = _help_advertised(help_text)
    dead = sorted(advertised & _flag_gated_tools("allow_web"))
    assert dead == [], (
        "every master_orchestrate worker run advertises %d web tool(s) that "
        "its own allow_web=False refuses: %s" % (len(dead), dead)
    )
