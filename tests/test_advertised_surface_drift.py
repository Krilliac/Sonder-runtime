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
import importlib
import inspect
import itertools
import os
import re
import sys
import textwrap

import server
import tool_capabilities as capabilities
from sonder_runtime.adapters import fleet_creations


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
    # server.py is size-capped, so newer tool families register themselves
    # from their own module with the same ``@mcp.tool()`` decorator (inside a
    # ``register(mcp, ...)`` function). Their source is part of the AST view.
    from sonder_runtime.bootstrap import computer_use_tools, openrouter_tools, playbooks

    module = ast.parse(inspect.getsource(server))
    nested = [node
              for registrar_module in (computer_use_tools, openrouter_tools, playbooks)
              for node in ast.walk(ast.parse(inspect.getsource(registrar_module)))
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    decorated = set()
    for node in list(module.body) + nested:
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


def _resolve_call_target(function, tree, expression):
    """The object a call inside ``function`` names, or a loud failure."""
    if isinstance(expression, ast.Attribute):
        owner = _resolve_call_target(function, tree, expression.value)
        return getattr(owner, expression.attr)
    if isinstance(expression, ast.Name):
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    if (alias.asname or alias.name) == expression.id:
                        module = importlib.import_module(node.module)
                        return getattr(module, alias.name)
        if expression.id in function.__globals__:
            return function.__globals__[expression.id]
    raise AssertionError("cannot follow %s out of %s" % (
        ast.unparse(expression), function.__qualname__,
    ))


def _agent_impl_call_sites(function, name="_agent_impl", _seen=None):
    """Every call site of ``name`` reachable from ``function``'s source.

    Returns ``{(filename, first_line, last_line)}``.  Follows the indirection
    the fleet worker uses: handing the callable to a helper
    (``repository_worker(..., agent_impl=_agent_impl)``) makes every call of
    that parameter inside the helper a call site too.  Any other reference
    to the callable -- an alias, a partial, a container -- fails loudly: a
    census that skipped it would certify a call it never saw.
    """
    seen = set() if _seen is None else _seen
    if (function, name) in seen:
        return set()
    seen.add((function, name))
    lines, first = inspect.getsourcelines(function)
    filename = os.path.normcase(os.path.realpath(inspect.getsourcefile(function)))
    tree = ast.parse(textwrap.dedent("".join(lines)))
    sites, followed = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        called = target.attr if isinstance(target, ast.Attribute) else getattr(
            target, "id", "",
        )
        if called == name:
            sites.add((filename, first + node.lineno - 1, first + node.end_lineno - 1))
            followed.add(id(target))
        handed = [*enumerate(node.args), *((kw.arg, kw.value) for kw in node.keywords)]
        for slot, value in handed:
            if slot is None or not (isinstance(value, ast.Name) and value.id == name):
                continue
            helper = _resolve_call_target(function, tree, target)
            if isinstance(slot, int):
                slot = list(inspect.signature(helper).parameters)[slot]
            sites |= _agent_impl_call_sites(helper, slot, seen)
            followed.add(id(value))
    stray = sorted(
        first + node.lineno - 1 for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and id(node) not in followed
    )
    assert not stray, (
        "%s uses %s on line(s) %s in a shape this census cannot follow"
        % (function.__qualname__, name, stray)
    )
    return sites


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


class _AgentReached(Exception):
    """Raised by the recording ``_agent_impl`` so no model is ever started."""


def _record_orchestrator_worker_agent_calls(monkeypatch, tmp_path):
    """Drive every ``_orchestrator_agent_worker`` path into a recording agent.

    Paths are enumerated, not listed: every combination of the factory's bool
    keywords (``build``), each against a caller-bound repository and against a
    host-provisioned greenfield root.  A shape the factory or worker refuses
    before any agent starts advertises nothing, but every combination must
    reach ``_agent_impl`` one way or its path went unchecked.
    Returns ``[(path, args, kwargs, (filename, line))]``.
    """
    calls = []

    def recording_agent_impl(*args, **kwargs):
        caller = sys._getframe(1)
        calls.append((args, kwargs, (caller.f_code.co_filename, caller.f_lineno)))
        raise _AgentReached

    monkeypatch.setattr(server, "_agent_impl", recording_agent_impl)
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: False)
    repository = tmp_path / "repository"
    repository.mkdir()
    creations = fleet_creations.create_workspace("drift-probe", 1, state_home=tmp_path)
    flags = tuple(
        name
        for name, parameter in inspect.signature(
            server._orchestrator_agent_worker
        ).parameters.items()
        if isinstance(parameter.default, bool)
    )
    recorded = []
    try:
        for combination in itertools.product((False, True), repeat=len(flags)):
            keywords = dict(zip(flags, combination, strict=True))
            reached = False
            for shape, project, assigned in (
                ("bound", str(repository), str(repository)),
                ("greenfield", "", str(creations.workers[0])),
            ):
                before = len(calls)
                try:
                    worker = server._orchestrator_agent_worker("code", project, **keywords)
                    worker("inspect the project", assigned)
                except _AgentReached:
                    pass
                except (RuntimeError, ValueError):
                    pass  # refused before any agent started: nothing advertised
                path = "%s %s" % (keywords, shape)
                recorded.extend((path, *call) for call in calls[before:])
                reached = reached or len(calls) > before
            assert reached, (
                "_orchestrator_agent_worker(%s) never reached _agent_impl" % keywords
            )
    finally:
        fleet_creations.release_workspace(creations)
    return recorded


def test_orchestrator_worker_help_names_no_tool_its_own_flags_refuse(
    monkeypatch, tmp_path,
):
    """F4: every fleet worker path pins ``allow_web=False``.

    ``_orchestrator_agent_worker`` hands ``_agent_impl`` to
    ``fleet_workers.repository_worker``, whose one call serves paths with
    different flags: repository fleets run ``read_only``; build fleets write in
    host-provisioned creation folders under a host tool allowlist that the
    transcript renders verbatim.  So the flags are captured from the real call
    on every path rather than restated here -- changing the call changes what
    this test checks -- and a static census proves that no call site went
    unexercised and no exercised call came from a site it never counted.
    """
    sites = _agent_impl_call_sites(server._orchestrator_agent_worker)
    assert sites, "no _agent_impl call reachable from _orchestrator_agent_worker"
    calls = _record_orchestrator_worker_agent_calls(monkeypatch, tmp_path)
    exercised = set()
    for path, _args, _kwargs, (filename, line) in calls:
        filename = os.path.normcase(os.path.realpath(filename))
        matched = {
            site for site in sites
            if site[0] == filename and site[1] <= line <= site[2]
        }
        assert matched, (
            "%s called _agent_impl from %s:%d, a site the census never found"
            % (path, filename, line)
        )
        exercised |= matched
    assert exercised == sites, (
        "no worker path exercised these _agent_impl call sites: %s"
        % sorted(sites - exercised)
    )

    web_gated = _flag_gated_tools("allow_web")
    assert web_gated
    dispatchable = capabilities.dispatch_names(server._agent_dispatch)
    turn = inspect.signature(server._agent_turn)
    for path, args, kwargs, _site in calls:
        bound = turn.bind(*args, **kwargs)
        bound.apply_defaults()
        run = bound.arguments
        assert run["allow_web"] is False, (path, run)
        project_scope, error = server._agent_project_scope(run["project"])
        assert project_scope and not error, (path, run["project"], error)
        # The gates _agent_turn renders the transcript's tool help through.
        gates = {
            "read_only": run["read_only"],
            "project_bound": bool(project_scope),
            "allow_web": run["allow_web"],
            "allow_location": run["allow_location"],
        }
        advertised = _help_advertised(server._agent_tool_help(**gates))
        assert advertised, "help went empty on %s" % path
        dead = sorted(advertised & web_gated)
        assert dead == [], (
            "master_orchestrate worker path %s advertises %d web tool(s) that "
            "its own allow_web=False refuses: %s" % (path, len(dead), dead)
        )
        refused = sorted(
            "%s (%s)" % (name, server._agent_run_tool_refusal(name, **gates))
            for name in advertised
            if server._agent_run_tool_refusal(name, **gates)
        )
        assert refused == [], (
            "master_orchestrate worker path %s advertises tool(s) its own run "
            "gates refuse: %s" % (path, refused)
        )
        if run["tool_allowlist"] is None:
            continue
        # Rendered as "HOST TOOL ALLOWLIST (cannot be expanded by the model)",
        # in the canonical spelling _agent_turn uses, so each name is a promise.
        allowlist = frozenset(
            server._canonical_agent_tool_name(name)
            for name in run["tool_allowlist"] if name
        )
        assert allowlist, "empty host tool allowlist on %s" % path
        broken = sorted(
            "%s (%s)" % (
                name,
                server._agent_run_tool_refusal(name, **gates) or "not dispatchable",
            )
            for name in allowlist
            if name not in dispatchable or server._agent_run_tool_refusal(name, **gates)
        )
        assert broken == [], (
            "master_orchestrate worker path %s renders a host tool allowlist "
            "naming tool(s) that cannot run on it: %s" % (path, broken)
        )
