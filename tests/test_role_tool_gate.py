"""Role tool gate (public-mode slices P1 and P2): a served ``user`` account
must not run host-acting tools, whatever the process-wide permission mode.

Design: ``D:\\sonder-eco\\_plans\\public-mode.md`` sections 2.3, 3 and 7.

Every tool handler on the legacy runtime is replaced by a recorder, so no test
here runs a real host action. The enumeration is the one the ``/<tool>`` path
itself uses (the registered MCP tool names, resolved through
``command_catalog.parse_invocation``), dispatched through
``serve._dispatch_catalogued_tool``.

``USER_CEILING`` is owned by this test, not by the implementation: it is the
largest set of tools a user-role account may ever reach (status and discovery
tools that read no host file, process, window or secret, and act on nothing).
An implementation allowlist that goes beyond it must be a reviewed edit to this
file; that is the point of the security review gate on P1.
"""

from __future__ import annotations

import json

import pytest

import permission_modes
import server
import sonder_runtime.interfaces.http.serve as serve
from sonder_runtime.adapters import command_catalog

MODES = tuple(permission_modes.MODES)

USER_CEILING = frozenset({
    "status",
    "admin_whoami",
    "tool_help",
    "tool_inventory",
    "command_registry_list",
    "tool_manifest",
    "tool_capability_manifest",
    # Served-account replacements that /<tool> substitutes for the two
    # manifest handlers (they filter by role; they run no host action).
    "_served_tool_manifest",
    "_served_tool_capability_manifest",
})

# Host-acting tools named in the audit; none may run for a user-role account.
HOST_ACTING = (
    "file_write", "file_edit", "file_delete", "file_move", "file_copy",
    "file_batch_write", "directory_create", "apply_patch", "run_code",
    "script_run", "run_project", "workspace_run", "isolated_run", "ui_action",
    "git_commit", "git_checkout", "git_branch", "git_stash", "git_tag",
    "build_run", "test_run", "dependency_add", "computer_task", "loop",
    "mission_start", "offload", "parallel_run_code", "scaffold_project",
)

_RECORDED_HELPERS = (
    "_ollama_pool_admin_page", "_served_task_tool",
    "_served_tool_manifest", "_served_tool_capability_manifest",
)


def _context(role):
    if role == "local-open":
        return {"mode": "local-open", "authorized": True, "api_key": False,
                "account": None}
    return {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": role, "role": role},
    }


def _set_mode(monkeypatch, mode):
    monkeypatch.setattr(permission_modes, "_STATE", {
        **permission_modes._STATE, "mode": mode, "elevated": False,
    })
    monkeypatch.setattr(permission_modes, "_LOADED", True)


def _registered_names():
    return [tool.name for tool in server.mcp._tool_manager.list_tools()]


def _run_matrix(monkeypatch, role, mode):
    """Dispatch ``/<tool>`` for every reachable tool; return (called, results)."""
    _set_mode(monkeypatch, mode)
    called = []
    names = _registered_names()
    for name in names:
        monkeypatch.setattr(
            server, name,
            (lambda n: lambda *a, **k: (called.append(n), "recorded")[1])(name),
            raising=False,
        )
    for helper in _RECORDED_HELPERS:
        monkeypatch.setattr(
            serve, helper,
            (lambda h: lambda *a, **k: (called.append(h), ("{}", 200))[1])(helper),
        )
    results = {}
    context = _context(role)
    for name in names:
        if command_catalog.parse_invocation("/" + name) is None:
            continue
        results[name] = serve._dispatch_catalogued_tool(
            "/" + name, serve.ConversationState(), context=context,
        )
    return set(called), results


def test_matrix_enumerates_the_whole_tool_surface():
    """Guard against a vacuous matrix: the catalog really is the big surface."""
    reachable = [
        n for n in _registered_names()
        if command_catalog.parse_invocation("/" + n) is not None
    ]
    assert len(reachable) >= 200
    for tool in HOST_ACTING:
        assert tool in reachable, tool


@pytest.mark.parametrize("mode", MODES)
def test_user_role_reaches_no_handler_outside_the_ceiling(monkeypatch, mode):
    called, results = _run_matrix(monkeypatch, "user", mode)
    leaked = sorted(called - USER_CEILING)
    assert not leaked, "user-role account ran handlers outside the ceiling under %s: %s" % (
        mode, leaked,
    )
    # Not vacuous: the matrix really dispatched every reachable tool, and the
    # host-acting ones came back as refusals rather than as silent no-ops.
    assert len(results) >= 200
    for tool in HOST_ACTING:
        assert str(results[tool]).startswith("refused"), (mode, tool, results[tool])


@pytest.mark.parametrize("mode,tool", (
    ("acceptEdits", "file_write"),
    ("auto", "file_write"),
    ("auto", "run_code"),
    ("auto", "script_run"),
    ("auto", "ui_action"),
))
def test_user_role_file_write_and_execution_refused_where_mode_would_allow(
    monkeypatch, mode, tool,
):
    """The reported hole: the mode allows it, the caller's role must not."""
    called, results = _run_matrix(monkeypatch, "user", mode)
    assert tool not in called
    assert str(results[tool]).startswith("refused")


@pytest.mark.parametrize("mode", MODES)
def test_http_tool_refusal_applies_role_before_the_permission_decision(monkeypatch, mode):
    """Deny-by-default must not depend on what the process-wide mode says."""
    _set_mode(monkeypatch, mode)
    decisions = []
    monkeypatch.setattr(
        serve.permission_policy, "decide_for_caller",
        lambda *a, **k: decisions.append(a) or None,  # None: "mode has no objection"
    )
    for tool in ("file_write", "run_code", "ui_action", "script_run"):
        refusal = serve._http_tool_refusal((tool,), "/" + tool, _context("user"))
        assert refusal.startswith("refused /" + tool), (mode, tool, refusal)
    assert decisions == [], "role gate must run before the permission decision"


@pytest.mark.parametrize("mode", MODES)
def test_user_role_slash_commands_that_map_to_host_tools_are_refused(monkeypatch, mode):
    """The chain-gated slash spellings share the same gate."""
    _set_mode(monkeypatch, mode)
    checked = 0
    for cmd, tools in command_catalog.http_slash_tools().items():
        tools = command_catalog.narrow_branch_tools(cmd, "", tools)
        outside = [t for t in tools if t not in USER_CEILING]
        if not outside:
            continue
        checked += 1
        refusal = serve._http_slash_refusal(cmd, "", context=_context("user"))
        assert refusal.startswith("refused"), (mode, cmd, outside, refusal)
    assert checked >= 10


def _handle_slash_literal_commands():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(serve.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_handle_slash")
    found = set()
    non_literal = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "cmd"):
            comp = node.comparators[0]
            if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                found.add(comp.value)
            elif (isinstance(comp, (ast.Tuple, ast.Set, ast.List))
                  and all(isinstance(e, ast.Constant) for e in comp.elts)):
                found |= {e.value for e in comp.elts if isinstance(e.value, str)}
            else:
                non_literal.append("line %d: %s" % (node.lineno, ast.unparse(node)))
    assert not non_literal, (
        "_handle_slash compares cmd to a non-literal, which no gate can "
        "enumerate: %s" % non_literal
    )
    return {c for c in found if c.startswith("/") and len(c) > 1}


def _first_prompt_name():
    from sonder_runtime.adapters import prompt_store
    return sorted(prompt_store.CATALOG)[0]


@pytest.mark.parametrize("mode", MODES)
def test_untooled_prompts_command_is_refused_for_user_only(monkeypatch, mode):
    _set_mode(monkeypatch, mode)
    name = _first_prompt_name()
    for argument in ("show " + name, "path " + name, "path", "reload", ""):
        refusal = serve._http_slash_refusal("/prompts", argument, context=_context("user"))
        assert refusal.startswith("refused /prompts"), (mode, argument, refusal)
        for role in ("developer", "admin", "local-open"):
            other = serve._http_slash_refusal("/prompts", argument, context=_context(role))
            assert "not available to this account role" not in other, (mode, role, argument, other)


@pytest.mark.parametrize("cmd,argument", [("/help", ""), ("/trace", "on"), ("/strict", "on")])
def test_untooled_session_commands_stay_allowed_for_user(monkeypatch, cmd, argument):
    _set_mode(monkeypatch, "default")
    assert serve._http_slash_refusal(cmd, argument, context=_context("user")) == ""


def test_every_chain_command_is_tooled_or_untooled_and_untooled_is_refused_by_default(monkeypatch):
    """A future branch that calls no tool is refused for user until reviewed."""
    from sonder_runtime.adapters.security import role_tool_policy
    _set_mode(monkeypatch, "default")
    tooled = set(command_catalog.http_slash_tools())
    untooled = set(command_catalog.http_slash_untooled())
    chain = _handle_slash_literal_commands()
    unclassified = sorted(chain - tooled - untooled)
    assert not unclassified, "chain commands in neither map: %s" % unclassified
    assert not (tooled & untooled)
    allowed = role_tool_policy.user_untooled_commands()
    assert allowed <= untooled, sorted(allowed - untooled)
    for cmd in sorted(untooled):
        refusal = serve._http_slash_refusal(cmd, "", context=_context("user"))
        if cmd in allowed:
            assert refusal == "", (cmd, refusal)
        else:
            assert refusal.startswith("refused"), "untooled %s is neither allowlisted nor refused" % cmd


def test_unparseable_chain_source_refuses_user_slash_commands(monkeypatch):
    """A dispatch source the catalog cannot read must refuse, not allow."""
    monkeypatch.setattr(command_catalog, "_source_path", lambda name: "Z:/nonexistent/" + name)
    command_catalog.reset_cache()
    try:
        for cmd in ("/prompts", "/help"):
            refusal = serve._http_slash_refusal(cmd, "", context=_context("user"))
            assert refusal.startswith("refused %s" % cmd), (cmd, refusal)
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()


def test_non_literal_cmd_comparison_makes_catalog_unavailable(monkeypatch, tmp_path):
    source = tmp_path / "chain.py"
    source.write_text(
        "NAMES = ('/x',)\ndef _handle_slash(cmd):\n    if cmd in NAMES:\n        return 1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(command_catalog, "_source_path", lambda name: str(source))
    with pytest.raises(command_catalog.CatalogUnavailable):
        command_catalog._branch_tool_calls("x.py", "_handle_slash", frozenset())


def test_untooled_refusal_names_a_command_not_a_tool():
    refusal = serve._http_slash_refusal("/prompts", "", context=_context("user"))
    assert "command '/prompts' is not available to this account role" in refusal


_LOOP_HOST_ACTIONS = ({"type": "file_write"}, {"type": "code"}, {"type": "project"})


def test_loop_actions_that_run_host_tools_are_refused_for_user():
    for action in _LOOP_HOST_ACTIONS:
        refusal = serve._loop_global_operation_refusal(
            json.dumps([action]), _context("user"),
        )
        assert refusal.startswith("refused /loop"), action
    refusal = serve._loop_global_operation_refusal(
        json.dumps(list(_LOOP_HOST_ACTIONS)), _context("user"),
    )
    assert refusal.startswith("refused /loop"), refusal


@pytest.mark.parametrize("role", ("developer", "admin", "local-open"))
def test_loop_precheck_is_unchanged_above_user(role):
    # These three actions are not system operations, so today's pre-check lets
    # every role above user through; the mode gate decides afterwards.
    payload = json.dumps(list(_LOOP_HOST_ACTIONS))
    assert serve._loop_global_operation_refusal(payload, _context(role)) == ""


# --- preserved behaviour: developer, admin and the local-open operator -----

_ADMIN_ONLY_AUTO = {
    "_ollama_pool_admin_page", "admin_accounts", "learn_preference",
    "permission_mode", "set_context_size", "tune_emotion_vectors",
    "update_emotion_vectors", "update_system_profile",
}


@pytest.mark.parametrize("role", ("developer", "admin", "local-open"))
def test_roles_above_user_keep_mode_governed_behaviour(monkeypatch, role):
    expectations = {
        "plan": (set(), {"file_write", "run_code", "script_run", "ui_action", "git_commit"}),
        "manual": (set(), {"file_write", "run_code", "script_run", "ui_action", "git_commit"}),
        "acceptEdits": ({"file_write", "git_commit", "loop"},
                        {"run_code", "script_run", "ui_action"}),
        "auto": ({"file_write", "run_code", "script_run", "ui_action", "git_commit", "loop"},
                 set()),
    }
    for mode, (must_run, must_not_run) in expectations.items():
        called, _results = _run_matrix(monkeypatch, role, mode)
        assert must_run <= called, (role, mode, sorted(must_run - called))
        assert not (must_not_run & called), (role, mode, sorted(must_not_run & called))
        assert len(called) >= 70, (role, mode, len(called))


def test_developer_and_admin_differ_only_by_the_declared_admin_operations(monkeypatch):
    developer, _ = _run_matrix(monkeypatch, "developer", "auto")
    admin, _ = _run_matrix(monkeypatch, "admin", "auto")
    assert developer <= admin
    assert admin - developer == _ADMIN_ONLY_AUTO


def test_local_open_operator_keeps_system_tools(monkeypatch):
    called, _ = _run_matrix(monkeypatch, "local-open", "auto")
    assert {"permission_mode", "admin_accounts", "file_write", "run_code"} <= called


# --- P2: chat control routes ------------------------------------------------

_CLICK = "click the Save button in paint"


@pytest.fixture
def chat_server(monkeypatch):
    """The real chat handler, with inference and every dispatch target recorded."""
    from tests.test_serve_auth import _http_server, _request

    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    legacy = serve._legacy_runtime()
    calls = []
    monkeypatch.setattr(legacy, "chat_web_response", lambda *a, **k: None)
    monkeypatch.setattr(
        legacy, "answer_with_history", lambda *a, **k: "plain model answer",
    )
    monkeypatch.setattr(legacy, "prewarm_model", lambda _model: None)
    monkeypatch.setattr(serve, "_server_side_history", lambda _session: [])
    monkeypatch.setattr(serve, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        legacy, "route_computer_use",
        lambda prompt: (calls.append(("route_computer_use", prompt)), "computer route reply")[1],
    )
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.computer_use_chat._dispatch",
        lambda _server, name, args: (calls.append(("_dispatch", name)), "{}")[1],
    )
    for name in ("file_write", "run_code", "ui_action", "computer_task"):
        monkeypatch.setattr(
            legacy, name, (lambda n: lambda *a, **k: (calls.append((n, "")), "ran")[1])(name),
            raising=False,
        )

    def post(role, prompt, mode="auto"):
        _set_mode(monkeypatch, mode)
        context = _context(role)
        monkeypatch.setattr(serve.Handler, "_request_auth_context", lambda self: context)
        body = json.dumps({
            "model": "sonder", "session": "role-gate-%s" % role,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        with _http_server(monkeypatch) as port:
            status, _headers, payload = _request(
                port, "POST", "/v1/chat/completions", body=body,
                headers={"Content-Type": "application/json"},
            )
        return status, payload, calls

    return post


def test_user_chat_turn_does_not_reach_the_computer_use_route(chat_server):
    status, payload, calls = chat_server("user", _CLICK)
    assert status == 200, payload
    assert [c for c in calls if c[0] in ("route_computer_use", "_dispatch")] == []
    content = json.loads(payload)["choices"][0]["message"]["content"]
    assert "computer route reply" not in content


def test_user_chat_turn_does_not_run_slash_tools_in_auto(chat_server):
    status, payload, calls = chat_server(
        "user", "/file_write path=role-gate-probe.txt content=x", mode="auto",
    )
    assert status == 200, payload
    assert [c for c in calls if c[0] in ("file_write", "run_code", "ui_action")] == []


def test_local_open_chat_turn_still_reaches_the_computer_use_route(chat_server):
    status, payload, calls = chat_server("local-open", _CLICK)
    assert status == 200, payload
    assert ("route_computer_use", _CLICK) in calls
    assert "computer route reply" in json.loads(payload)["choices"][0]["message"]["content"]


@pytest.mark.parametrize("role", ("developer", "admin"))
def test_developer_and_admin_chat_turns_still_reach_the_computer_use_route(chat_server, role):
    status, payload, calls = chat_server(role, _CLICK)
    assert status == 200, payload
    assert ("route_computer_use", _CLICK) in calls


def test_computer_use_chat_route_itself_is_unchanged_for_the_operator(monkeypatch):
    """The packaged adapter is shared with the trusted surface: no role param."""
    from sonder_runtime.bootstrap import computer_use_chat

    dispatched = []
    monkeypatch.setattr(
        computer_use_chat, "_dispatch",
        lambda _server, name, args: (dispatched.append(name), "{}")[1],
    )
    result = computer_use_chat.route("stop controlling", object())
    assert dispatched == ["computer_use_stop"]
    assert result == "{}"
