"""Cross-surface tool-contract conformance.

The per-surface gate files (`test_permission_gate_*`) each pin one dispatch
chain. What none of them pin is the *agreement between surfaces*: the same
canonical tool, reached by a different spelling -- a `/loop` action name, a
generated catalog route, a freshly registered name -- must meet the same
authority boundary as its curated spelling. Every previous instance of this
regression class (`/runwindow`, `/training`, `/emotion`) was found by hand;
this file is the check that finds the next one by construction.

The contract is derived, not restated: `tool_contract` reads the runtime's own
declarations (`sonder_serve.SYSTEM_OPERATION_TOOLS`,
`server._AGENT_SYSTEM_OPERATOR_TOOLS`, `permission_modes` sets, the command
catalog, the loop-action vocabulary) and answers one question the HTTP gates
consult -- which role-gated operation does this tool perform -- with a
deny-by-default answer (`SYSTEM_OPERATION_UNBOUND`) when the declarations
drift apart. Tests here exercise the REAL gates with synthetic auth contexts,
never re-implementations.
"""
from __future__ import annotations

import inspect
import json

import pytest

import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as serve

pytestmark = pytest.mark.unit


def _context(role):
    return {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": role, "role": role},
    }


LOCAL_OPEN = {
    "mode": "local-open", "authorized": True, "api_key": False,
    "account": None,
}


@pytest.fixture(autouse=True)
def _gate_sandbox(monkeypatch, tmp_path):
    """Deterministic gate inputs: manual mode, no operator rules, no disk.

    The refusals under test are *authority* refusals; the permission mode must
    not be the thing deciding. `manual` with `interactive=False` degrades ask
    to allow, so anything refused below is refused by a role boundary or the
    durable-authority class -- exactly what this file is about.
    """
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    monkeypatch.setattr(pm, "_LOADED", True)
    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    monkeypatch.setitem(pm._STATE, "elevated", False)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)


# --- P1: privileged closure over HTTP -------------------------------------


def test_every_system_operator_tool_is_refused_for_an_ordinary_account():
    """The parity the two hand-maintained lists never had.

    `server._AGENT_SYSTEM_OPERATOR_TOOLS` is the runtime's own declaration of
    "this is a system operation; an agent is never that operator". A served
    ordinary account is not that operator either, and must be stopped at the
    HTTP boundary -- by a `SYSTEM_OPERATION_TOOLS` role binding, by the
    durable-authority non-degrade, or by the deny-by-default rule for a
    declared-but-unbound name. Reaching the tool body and relying on its
    in-tool token check is how `admin_accounts` sat open.
    """
    for tool in sorted(server._AGENT_SYSTEM_OPERATOR_TOOLS):
        refusal = serve._http_tool_refusal((tool,), "/" + tool, _context("user"))
        assert refusal, (
            "%s is agent-refused as a system operation but sails past the "
            "HTTP boundary for an ordinary served account" % tool
        )


def test_an_unbound_system_operation_still_passes_the_trusted_surfaces():
    """Deny-by-default must not take authority away from the operator.

    `local-open` is the single-user surface and the owner api-key is the
    administrator credential; both keep every system tool, bound or unbound.
    """
    owner_key = {
        "mode": "api-key", "authorized": True, "api_key": True, "account": None,
    }
    for tool in sorted(server._AGENT_SYSTEM_OPERATOR_TOOLS):
        for context in (LOCAL_OPEN, owner_key):
            refusal = serve._http_tool_refusal((tool,), "/" + tool, context)
            assert "authorization" not in refusal, (tool, context["mode"])


# --- P2: a loop-action spelling meets the tool's own boundary --------------


def test_loop_action_spelling_carries_the_same_role_as_the_tools_own_name():
    """`/memory_privacy_repair` requires the developer role; the identical
    work spelled `{"type": "memory_privacy_repair"}` inside a `/loop` payload
    must not run for an ordinary account. This was live: the hand-kept
    `_LOOP_GLOBAL_OPERATION_TYPES` named 4 of the 7 loop-reachable system
    operations, and this tool was one of the other three.
    """
    for action in ("memory_privacy_repair", "memory_quality_repair"):
        payload = json.dumps({"actions": [{"type": action}]})
        refusal = serve._loop_global_operation_refusal(payload, _context("user"))
        assert "authorization" in refusal, action


def test_loop_action_spelling_of_selfmod_repair_requires_admin():
    """`self_heal_repair` is `selfmod_deploy` (admin) under its own name; a
    developer spelling it as a loop action must meet the same boundary, not
    the weaker task-scope refusal that happened to catch accounts."""
    payload = json.dumps({"actions": [{"type": "self_heal_repair"}]})
    refusal = serve._loop_global_operation_refusal(payload, _context("developer"))
    assert "administrator authorization" in refusal


def test_loop_alias_spellings_still_meet_the_admin_boundary():
    """The four the old hand map did cover must stay covered once derived."""
    for action in ("emotion_update", "emotion_tune", "learn_preference", "unload"):
        payload = json.dumps({"actions": [{"type": action}]})
        refusal = serve._loop_global_operation_refusal(payload, _context("developer"))
        assert "administrator authorization" in refusal, action


def test_loop_payloads_stay_open_for_the_trusted_surfaces():
    """The closure keys on served authority: local-open and the owner key
    lose nothing, and ordinary read/code actions stay open for everyone."""
    admin_payload = json.dumps({"actions": [{"type": "memory_privacy_repair"}]})
    assert serve._loop_global_operation_refusal(admin_payload, LOCAL_OPEN) == ""
    assert serve._loop_global_operation_refusal(admin_payload, _context("admin")) == ""
    benign = json.dumps({"actions": [{"type": "code", "code": "print(1)"}]})
    assert serve._loop_global_operation_refusal(benign, _context("user")) == ""
    # Malformed payloads keep reaching server.loop's own JSON contract.
    assert serve._loop_global_operation_refusal("not json", _context("user")) == ""


# --- the classifier the gates consult --------------------------------------


def test_a_declared_system_operation_without_a_binding_is_classified_unbound():
    """Injected drift, not live drift: the live maps are required to be clean
    (see `test_validate_contracts_is_clean_on_the_live_declarations`), so the
    deny-by-default answer is proven by handing the classifier a mutated
    binding map with one entry deleted."""
    import tool_contract

    without_binding = {
        k: v for k, v in serve.SYSTEM_OPERATION_TOOLS.items()
        if k != "admin_set_account"
    }
    assert tool_contract.system_operation_for(
        "admin_set_account", operation_tools=without_binding,
    ) == tool_contract.SYSTEM_OPERATION_UNBOUND


def test_a_bound_tool_answers_its_declared_operation():
    import tool_contract

    assert tool_contract.system_operation_for("permission_mode") == "permission_mode_change"
    assert tool_contract.system_operation_for("/permission_mode") == "permission_mode_change"


def test_an_ordinary_tool_answers_no_operation():
    import tool_contract

    assert tool_contract.system_operation_for("file_read") == ""
    assert tool_contract.system_operation_for("") == ""


def test_the_classifier_canonicalizes_agent_alias_spellings():
    """`master` is `master_orchestrate` on the agent surface; a classifier
    that graded raw spellings would answer "" for every alias of a bound or
    declared tool."""
    import tool_contract

    for alias, target in server._AGENT_TOOL_ALIASES.items():
        assert tool_contract.system_operation_for(alias) == (
            tool_contract.system_operation_for(target)
        ), alias


# --- P7: the ledger's key vocabulary covers the text redactor's -------------


def test_the_ledger_masks_every_name_the_text_redactor_treats_as_secret():
    """JSON splits a keyword and its value into separate strings, so the
    free-text regex can never see the pair; the key vocabulary was the only
    line of defence and it had drifted narrower than the regex --
    `{"pwd": ...}` survived verbatim into the (detail-gated) ledger."""
    for key in ("pwd", "passwd", "credential", "authorization",
                "access_key", "apikey", "password", "token", "secret"):
        safe = activity_tracker._safe_args({key: "hunter2-value"})
        assert "hunter2-value" not in json.dumps(safe), key


def test_the_argv_mask_hides_values_for_the_same_secret_flags():
    safe = activity_tracker._safe_command(["deploy", "--pwd", "hunter2-value"])
    assert "hunter2-value" not in safe
    safe = activity_tracker._safe_command(["run", "credential=hunter2-value"])
    assert "hunter2-value" not in safe


def test_the_marker_vocabulary_covers_the_text_regexes_keywords():
    """The two masks and the free-text regex must share one vocabulary.

    Each simple keyword is asserted to still be one the regex recognises, so
    if the regex ever *drops* a word this test's own premise fails loudly
    instead of silently pinning a stale list.
    """
    for word in ("password", "passwd", "pwd", "token", "secret",
                 "authorization", "credential"):
        assert activity_tracker._SECRET_ASSIGNMENT_RE.search(
            "%s=hunter2value" % word
        ), "regex no longer recognises %r; update this test's vocabulary" % word
        assert word in activity_tracker._SENSITIVE_NAME_MARKERS, word
    for form in ("api_key", "api-key", "apikey",
                 "access_key", "access-key", "accesskey"):
        assert form in activity_tracker._SENSITIVE_NAME_MARKERS, form


# --- P8: the declarations cannot rot silently ------------------------------


def test_validate_contracts_is_clean_on_the_live_declarations():
    """The central drift check, run against the real maps.

    A tool the runtime declares to be a system operation must either carry an
    HTTP role binding or be durable-authority (refused non-interactively
    everywhere). `admin_accounts` was declared and neither -- the
    deny-by-default rule kept it admin-only at runtime, but the *declaration*
    stays reportable drift until someone binds it.
    """
    import tool_contract

    assert tool_contract.validate_contracts() == ()


def test_validate_contracts_reports_injected_drift():
    """The detector must be able to fail, or the clean run above proves
    nothing: hand it declarations with one system operation unbound and one
    binding the agent path does not refuse."""
    import tool_contract

    issues = tool_contract.validate_contracts(
        operator_tools=frozenset(server._AGENT_SYSTEM_OPERATOR_TOOLS | {"file_read"}),
    )
    assert any("file_read" in issue for issue in issues)

    issues = tool_contract.validate_contracts(
        operation_tools={**serve.SYSTEM_OPERATION_TOOLS, "file_read": "account_management"},
    )
    assert any("file_read" in issue for issue in issues)

    issues = tool_contract.validate_contracts(
        operation_tools={**serve.SYSTEM_OPERATION_TOOLS,
                         "permission_mode": "not_a_declared_operation"},
    )
    assert any("not_a_declared_operation" in issue for issue in issues)


def test_contracts_cover_every_registered_and_declared_name():
    import tool_contract

    rows = tool_contract.contracts()
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert registered <= set(rows)
    assert set(server._AGENT_SYSTEM_OPERATOR_TOOLS) <= set(rows)

    contract = rows["permission_mode"]
    assert contract.registered
    assert contract.http_operation == "permission_mode_change"
    assert contract.http_role == "admin"
    assert contract.agent_operator
    read = rows["file_read"]
    assert read.http_operation == ""
    assert read.http_role == ""
    assert not read.agent_operator


_AUTHORITY_GRAMMAR_PREFIXES = (
    "admin_", "permission_", "runtime_policy_", "autopilot_", "workflow_",
    "memory_privacy_", "memory_quality_",
)
_AUTHORITY_GRAMMAR_NAMES = frozenset({
    "elevate", "unload", "set_context_size", "self_heal_repair",
    "update_system_profile", "learn_preference", "update_emotion_vectors",
    "tune_emotion_vectors", "memory_export",
})

# Registered tools the grammar matches that are deliberately NOT authority
# gated. Every entry is a claim about the tool body, checked by reading it;
# the dead-entry test below keeps this from collecting names that no longer
# exist. This is the completeness tripwire for NEW registrations: a tool
# named into the authority vocabulary that lands in no declared set and has
# no reasoned entry here fails the suite instead of shipping open.
_AUTHORITY_GRAMMAR_VERIFIED_EXEMPT = {
    "admin_status": "read-only safety summary; no account data beyond counts",
    "admin_whoami": "shows only the caller's own token's account",
    "admin_login": "durable-authority: refused non-interactively everywhere",
    "admin_register": "durable-authority: refused non-interactively everywhere",
    "admin_private_chain_of_thought": (
        "double opt-in enforced in-tool: SONDER_ALLOW_PRIVATE_COT plus an "
        "explicit allow rule for its own name (docs/wiki/09-security-model)"
    ),
    "permission_policy": "formats the on-disk rule policy; read-only",
    "permission_mode": "bound: permission_mode_change",  # here for the dead-entry sweep
    "runtime_policy_status": "formats the runtime policy; read-only",
    "autopilot_status": "read-only progress report",
    "workflow_list": "lists saved workflow names; read-only",
    "memory_privacy_review": "read-only preview of what repair would change",
    "memory_quality_report": "read-only quality metrics",
}


def test_every_authority_shaped_registration_is_declared_or_exempt():
    """A new `admin_*`/`permission_*`/… registration must be classified
    somewhere on arrival: an HTTP role binding, the agent operator set,
    durable authority, or a reasoned exemption above. Absence from all of
    them is exactly how a privileged-shaped tool ships reachable by every
    served account."""
    import permission_modes
    import tool_contract

    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    unclassified = []
    for name in sorted(names):
        if not (name.startswith(_AUTHORITY_GRAMMAR_PREFIXES)
                or name in _AUTHORITY_GRAMMAR_NAMES):
            continue
        declared = (
            bool(tool_contract.system_operation_for(name))
            or name in permission_modes.DURABLE_AUTHORITY_TOOLS
        )
        if not declared and name not in _AUTHORITY_GRAMMAR_VERIFIED_EXEMPT:
            unclassified.append(name)
    assert not unclassified, (
        "authority-shaped tool(s) with no declared boundary and no reasoned "
        "exemption: %s" % ", ".join(unclassified)
    )


def test_tool_contract_ships_in_the_packaged_payload():
    """sonder_serve imports tool_contract at module level, so a payload
    missing it dies exactly the way the packager's own comment describes:
    in a detached process whose log the GUI never reads. REQUIRED_FILES is
    the loud-failure list; a load-bearing gate module belongs on it, the
    way tool_capabilities.py already is."""
    from scripts import package_local_system as package

    assert "tool_contract.py" in package.REQUIRED_FILES


def test_tool_contract_is_reloaded_with_the_served_authority_gate(monkeypatch):
    """A deployed authority-policy edit must replace the HTTP process module."""
    import sonder_runtime.interfaces.http.serve as sonder_serve

    original = sonder_serve.tool_contract
    replacement = object()
    monkeypatch.setattr(
        sonder_serve.live_reload,
        "reload_changed_modules",
        lambda names: {"tool_contract": replacement},
    )
    try:
        sonder_serve._maybe_live_reload()
        assert sonder_serve.tool_contract is replacement
        assert "tool_contract" in sonder_serve.LIVE_RELOAD_MODULES
        assert "tool_contract" in server.LIVE_RELOAD_MODULES
    finally:
        sonder_serve.tool_contract = original


def test_diagnostics_reports_contract_drift_without_enforcement():
    """Drift must be operator-visible in the same place the shadow registry's
    is, and the clean verdict must say what deny-by-default covers."""
    source = inspect.getsource(server.diagnostics)
    assert "tool_contract_report()" in source
    assert "tool contract" in source

    report = server.tool_contract_report()
    assert report.startswith("clean:")
    assert "deny by default" in report


def test_the_exemption_list_has_no_dead_entries_and_every_reason_is_real():
    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    dead = set(_AUTHORITY_GRAMMAR_VERIFIED_EXEMPT) - names
    assert not dead, "exempt entries for tools that no longer exist: %s" % dead
    for name, reason in _AUTHORITY_GRAMMAR_VERIFIED_EXEMPT.items():
        assert reason and len(reason) > 12, name


# --- P2: alias closure across the catalog ----------------------------------


def test_every_catalog_spelling_resolves_to_its_commands_canonical_tool():
    import command_catalog

    for command in command_catalog.catalog():
        if not command.tool:
            continue
        for name in command.all_names:
            hit = command_catalog.by_name(name)
            assert hit is not None and hit.tool == command.tool, name


# The four slash groups whose bare form deliberately narrows to a read-only
# status tool before the gate (see sonder_serve._http_slash_refusal); their
# bare spellings are *supposed* to answer for ordinary accounts.
_READ_NARROWED_SLASHES = frozenset({
    "/emotion", "/emotions", "/vectors", "/mood",
    "/prefer", "/preference", "/preferences",
    "/contextsize", "/ctxsize", "/runtime", "/models",
})


def test_every_fully_bound_slash_spelling_is_refused_for_ordinary_accounts():
    """Alias closure at the curated HTTP chain: every slash spelling whose
    whole tool set is authority-bound (or durable) refuses an ordinary
    account, whatever the spelling."""
    import command_catalog
    import permission_modes
    import tool_contract

    checked = []
    for slash, tools in sorted(command_catalog.http_slash_tools().items()):
        if slash in _READ_NARROWED_SLASHES or not tools:
            continue
        if not all(
            tool_contract.system_operation_for(tool)
            or tool in permission_modes.DURABLE_AUTHORITY_TOOLS
            for tool in tools
        ):
            continue
        refusal = serve._http_slash_refusal(slash, "", _context("user"))
        assert refusal, slash
        checked.append(slash)
    assert len(checked) >= 3, (
        "vacuity control: the sweep found almost nothing bound (%r) -- the "
        "derivation it walks has probably changed shape" % checked
    )


def test_an_unbound_durable_tool_keeps_its_actionable_refusal():
    """`admin_login` is durable-authority and deliberately unbound; the
    refusal an ordinary account sees must be the durable one that names the
    remedy, not the generic unbound-system-operation message."""
    refusal = serve._http_tool_refusal(("admin_login",), "/login", _context("user"))
    assert "console" in refusal or "allow rule" in refusal, refusal


def test_a_binding_deleted_at_runtime_fails_closed_for_served_accounts(every_tool_allowed_by_rule, monkeypatch):
    """The deny-by-default rule at the real boundary, proven by deleting a
    binding: the tool stays declared (agent operator set) but unbound, and a
    served non-admin caller is refused rather than passed to the mode gate's
    non-interactive degrade."""
    monkeypatch.setattr(
        serve, "SYSTEM_OPERATION_TOOLS",
        {k: v for k, v in serve.SYSTEM_OPERATION_TOOLS.items() if k != "unload"},
    )
    refusal = serve._http_tool_refusal(("unload",), "/unload", _context("developer"))
    assert "unclassified system operation" in refusal
    assert serve._http_tool_refusal(("unload",), "/unload", LOCAL_OPEN) == ""
    assert serve._http_tool_refusal(("unload",), "/unload", _context("admin")) == ""


# --- P5: the role matrix binds per tool at the real boundary ---------------


def test_the_role_matrix_binds_per_tool_at_the_real_boundary():
    for tool, operation in sorted(serve.SYSTEM_OPERATION_TOOLS.items()):
        role = serve.SYSTEM_OPERATION_ROLES[operation]
        assert serve._http_tool_refusal((tool,), "/" + tool, _context("user")), tool
        developer = serve._http_tool_refusal((tool,), "/" + tool, _context("developer"))
        assert ("authorization" in developer) is (role == "admin"), (tool, developer)
        admin = serve._http_tool_refusal((tool,), "/" + tool, _context("admin"))
        assert "authorization" not in admin, (tool, admin)


# --- P4: local-open keeps its capability model, not an exemption -----------


def test_local_open_is_still_bound_by_plan_mode_and_deny_rules(monkeypatch):
    """local-open passes every ROLE boundary (single-operator surface) but
    the autonomy mode and explicit rules still bind there -- broader
    capability, not a hole in the gate."""
    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    refusal = serve._http_tool_refusal(("file_write",), "/file_write", LOCAL_OPEN)
    assert "plan" in refusal

    monkeypatch.setitem(pm._STATE, "mode", pm.MANUAL)
    monkeypatch.setattr(
        pm, "_rule_lookup",
        lambda tool: {"action": "deny", "pattern": "file_write"}
        if tool == "file_write" else None,
    )
    refusal = serve._http_tool_refusal(("file_write",), "/file_write", LOCAL_OPEN)
    assert "rule" in refusal


# --- P6: typed input fails before dispatch ---------------------------------


def test_an_unknown_keyword_never_reaches_the_handler(monkeypatch):
    def never_runs(**_kwargs):
        raise AssertionError("handler must not run for a rejected argument")

    monkeypatch.setattr(serve.server, "file_read", never_runs)
    out = serve._dispatch_catalogued_tool(
        "/file_read path=x bogus_key=1", serve.ConversationState(),
        context=_context("user"),
    )
    assert out is not None and "does not take" in out


def test_agent_args_must_be_a_json_object():
    out = server._agent_dispatch("file_read", "not-a-dict")
    assert out.startswith("ERROR:") and "JSON object" in out


def test_an_unknown_tool_name_dispatches_nothing_anywhere():
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    out = serve._dispatch_catalogued_tool(
        "/definitely_not_a_tool_xyz arg", serve.ConversationState(),
        context=_context("user"),
    )
    assert out is None  # falls through to the model; nothing dispatched

    agent = server._agent_dispatch("definitely_not_a_tool_xyz", {})
    assert agent.startswith("ERROR:") and "definitely_not_a_tool_xyz" in agent

    with pytest.raises(ToolError):
        asyncio.run(server.mcp.call_tool("definitely_not_a_tool_xyz", {}))


def test_the_mcp_surface_validates_argument_types_before_running():
    import asyncio

    with pytest.raises(Exception) as excinfo:
        asyncio.run(server.mcp.call_tool("admin_accounts", {"limit": "not-an-int"}))
    assert "limit" in str(excinfo.value) or "valid" in str(excinfo.value).lower()


# --- the indirect path: a saved workflow replays through the same gate -----


def test_a_saved_workflow_replay_meets_the_same_per_action_gate(monkeypatch, tmp_path):
    """workflow_run replays saved actions through `_loop_dispatch`, so the
    per-action permission gate must hold on replay exactly as it does for a
    live `/loop` payload: `plan` refuses the dangerous-class action.

    The store is rehomed to tmp the way `test_workflows.py` does it -- the
    default store is the workspace root's `workflows.json`, a tracked file
    this test must not touch.
    """
    from sonder_runtime.adapters.filesystem import workflow_store

    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    saved = server.workflow_save(
        "contract_probe_flow",
        json.dumps([{"type": "memory_privacy_repair"}]),
        "conformance probe",
    )
    assert "contract_probe_flow" in saved
    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    out = server.workflow_run("contract_probe_flow")
    assert "refused by the permission gate" in out or "HOST POLICY" in out
