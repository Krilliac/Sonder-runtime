"""The read-only workbench family runs through the typed tool gateway on every surface.

``directory_tree``, ``file_find``, ``file_read``, ``file_read_range``,
``text_search``, ``script_search`` and ``program_search`` reach the guarded
primitives only through ``application.tools`` -- from the native MCP surface
directly, and from the seven legacy ``server`` handlers through
``server._typed_tool``. One pipeline (schema, resource policy, the
runtime's permission modes, deadline and cancellation, the packaged guards,
redaction) and one durable, hash-chained receipt per call whatever the
outcome, with the receipt naming how the call ended and where it came from.

The permission decision is made once per call: by the gateway for the native
surface, and by the legacy surface's own gate (console, MCP, HTTP, agent) for
a legacy forward, which the gateway records as ``permission:surface``.
"""
from __future__ import annotations

import ast
import io
import json
import pathlib
import time
from unittest.mock import Mock

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.filesystem import file_ops, workbench
from sonder_runtime.adapters.persistence.tool_audit import (
    DurableToolAuditRepository,
    ToolAuditLimits,
)
from sonder_runtime.adapters.security.permission_evaluator import PermissionModesEvaluator
from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import (
    TERMINAL_STATES,
    ToolGatewayRequest,
    ToolPermission,
    ToolReceipt,
    ToolScope,
)
from sonder_runtime.application.tools.resource_policy import ResourcePolicy
from sonder_runtime.application.tools.typed_gateway import default_tool_context
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.bootstrap import typed_tools
from sonder_runtime.bootstrap.native_mcp import run_native_mcp
from sonder_runtime.domain.common.errors import (
    Cancelled,
    DeadlineExceeded,
    Forbidden,
    InvalidInput,
)
from sonder_runtime.platform import paths as runtime_paths

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.txt").write_text("needle\nsecond line\nthird\n", encoding="utf-8")
    (root / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=do-not-read\n", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    return root


@pytest.fixture
def application(tmp_path, monkeypatch, workspace):
    """A real application graph whose audit file lives under a private home."""
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    app = bootstrap_app.build_application()
    monkeypatch.setattr(server, "_APP_GRAPH", app)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    try:
        yield app
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


def _audit(tmp_path):
    return DurableToolAuditRepository(tmp_path / "home" / "audit" / "tool-receipts.jsonl")


def _native(app, name, arguments):
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2.0", "capabilities": {"tools": {}}}}
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    output = io.StringIO()
    run_native_mcp(
        app,
        input_stream=io.StringIO(json.dumps(initialize) + "\n" + json.dumps(call) + "\n"),
        output_stream=output,
    )
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    return rows[1]["result"]


# (legacy handler, its keyword arguments, native tool name, native arguments
# spelling out every default the legacy handler fills in, so the two surfaces
# hand the executor identical arguments)
PARITY = [
    ("file_find", {"query": "notes.txt"},
     "file_find", {"query": "notes.txt", "root": "", "max_results": 50, "include_ignored": False}),
    ("file_read", {"path": "notes.txt"},
     "file_read", {"path": "notes.txt", "max_bytes": 256000}),
    ("directory_tree", {"path": "."},
     "directory_tree", {"path": ".", "depth": 2, "max_entries": 200,
                        "include_hidden": False, "include_ignored": False}),
    ("file_read_range", {"path": "notes.txt", "start_line": 2, "end_line": 2},
     "file_read_range", {"path": "notes.txt", "start_line": 2, "end_line": 2}),
    ("text_search", {"query": "needle", "root": "."},
     "text_search", {"query": "needle", "root": ".", "glob": "*", "regex": False,
                     "case_sensitive": False, "max_results": 100, "max_entries": 20000,
                     "timeout_seconds": 10.0, "include_hidden": False,
                     "include_ignored": False}),
    ("script_search", {"query": "tool.py", "root": "."},
     "script_search", {"query": "tool.py", "root": ".", "max_results": 100,
                       "max_entries": 20000, "timeout_seconds": 10.0,
                       "include_hidden": False, "include_ignored": False}),
    ("program_search", {"query": "python*", "max_results": 5},
     "program_search", {"query": "python*", "max_results": 5}),
]


@pytest.mark.parametrize("legacy_name, legacy_args, native_name, native_args", PARITY)
def test_both_surfaces_run_one_pipeline_and_leave_one_receipt_each(
    application, tmp_path, monkeypatch, legacy_name, legacy_args, native_name, native_args,
):
    # Search output includes measured elapsed_ms, and the receipt deliberately
    # hashes that exact output. Equal filesystem input alone does not imply
    # identical output on two successive calls. Control only the filesystem
    # adapter's clock for this surface-parity test; retain real gateway clocks
    # and the complete output digest (including scan timing).
    adapter_clock = Mock(wraps=time)
    adapter_clock.monotonic.return_value = 1000.0
    monkeypatch.setattr(workbench, "time", adapter_clock)
    legacy = getattr(server, legacy_name)(**legacy_args)
    native = _native(application, native_name, native_args)

    assert not legacy.startswith("ERROR"), legacy
    assert native["isError"] is False, native
    assert native["evidence"]["terminal"] == "completed"

    records = _audit(tmp_path).read()
    assert len(records) == 2
    by_source = {record["source"]: record for record in records}
    assert set(by_source) == {"repl", "mcp"}
    assert by_source["mcp"]["result_digest"] == by_source["repl"]["result_digest"], (
        "the two surfaces graded the same call differently")
    assert by_source["mcp"]["result_digest"] == native["evidence"]["result_digest"]
    for record in records:
        assert record["terminal"] == "completed" and record["success"] is True
        assert record["policy_match"].startswith("resource:read-only:")
        assert "permission:" in record["policy_match"]
        assert record["model"] == ""
    _audit(tmp_path).verify()


def test_the_legacy_output_is_the_data_the_gateway_returned(application, tmp_path):
    out = server.file_read("notes.txt")
    assert "second line" in out and "3 lines" not in out  # the legacy formatter, unchanged
    record = _audit(tmp_path).read()[-1]
    assert record["tool_name"] == "read_file"
    assert record["auth_level"] == "local"
    assert record["evidence"]["truncated"] is False

    ranged = server.file_read_range("notes.txt", start_line=2, end_line=2)
    assert ranged.splitlines()[1].strip() == "2  second line"


def test_a_path_outside_the_roots_is_refused_the_same_way_on_both_surfaces(
    application, tmp_path,
):
    outside = str(tmp_path / "elsewhere.txt")
    (tmp_path / "elsewhere.txt").write_text("x", encoding="utf-8")

    legacy = server.file_read(outside)
    native = _native(application, "file_read", {"path": outside})

    assert legacy.startswith("ERROR: ")
    assert native["isError"] is True
    assert legacy[len("ERROR: "):] == native["output"]
    records = _audit(tmp_path).read()
    assert [record["terminal"] for record in records] == ["failed", "failed"]
    assert all(record["success"] is False for record in records)


def test_the_native_surface_now_enforces_the_secret_read_guard_on_line_ranges(
    application, tmp_path,
):
    native = _native(application, "file_read_range",
                     {"path": ".env", "start_line": 1, "end_line": 1})
    legacy = server.file_read_range(".env", start_line=1, end_line=1)

    assert native["isError"] is True and "protected" in native["output"]
    assert legacy.startswith("ERROR: ") and "protected" in legacy
    assert "do-not-read" not in native["output"] + legacy


def test_permission_modes_governs_the_typed_path_on_both_surfaces(
    application, tmp_path, monkeypatch,
):
    """Native: the gateway decides. Legacy: the surface decided, the gateway records it."""
    monkeypatch.setattr(
        pm, "_rule_lookup",
        lambda tool: ({"pattern": tool, "action": pm.DENY, "note": "test"}
                      if tool == "file_find" else None),
    )
    native = _native(application, "file_find", {"query": "notes.txt"})
    assert native["isError"] is True and native["error"] == "permission_denied"
    assert native["evidence"]["source"] == "rule"
    record = _audit(tmp_path).read()[-1]
    assert record["terminal"] == "policy_denied"
    assert record["policy_match"] == "permission:rule" and record["output"] == ""

    # The legacy chain gates at its entry point, before the handler runs...
    refused = server.control_command("/file_find query=notes.txt")
    assert refused.startswith("refused /file_find")
    assert len(_audit(tmp_path).read()) == 1, "a refused legacy call never reached the gateway"
    # ...and a legacy forward that does run is not decided a second time: an
    # internal Python call is deliberately ungated, and a console call an
    # operator answered yes to must not be refused by an unattended re-decision.
    out = server.file_find(query="notes.txt")
    assert not out.startswith("ERROR")
    record = _audit(tmp_path).read()[-1]
    assert record["source"] == "repl"
    assert record["policy_match"].endswith("permission:surface")


def test_plan_mode_still_reads_because_the_family_is_safe(application, monkeypatch):
    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    native = _native(application, "text_search", {"query": "needle", "root": "."})
    assert native["isError"] is False
    assert "needle" in native["output"]


def test_read_file_is_graded_by_its_catalog_name(application, monkeypatch):
    """The typed name ``read_file`` must be decided as ``file_read``, not as unclassified."""
    seen = []
    real = pm.decide_for_caller

    def spy(tool_name, **kwargs):
        seen.append((tool_name, kwargs["surface"], kwargs["gate_control_exempt"]))
        return real(tool_name, **kwargs)

    monkeypatch.setattr(pm, "decide_for_caller", spy)
    assert _native(application, "file_read", {"path": "notes.txt"})["isError"] is False
    assert ("file_read", "native-mcp", False) in seen


# --- the gateway's own guarantees, without a surface --------------------------


def _facade(tmp_path, *, policy=None, permissions=()):
    audit = DurableToolAuditRepository(tmp_path / "gateway-audit.jsonl")
    return ToolApplicationFacade.compose(
        typed_tools.typed_tool_registry(), PackagedToolExecutor(),
        policy=policy if policy is not None else typed_tools.typed_tool_policy(),
        receipts=ReceiptStore(), audit=audit, permissions=permissions,
    ), audit


def _request(tool="program_search", arguments=None, **overrides):
    values = dict(
        request_id="req-1", tool_name=tool,
        arguments=arguments if arguments is not None else {"query": "python*"},
        scope=ToolScope("owner", (), frozenset(), source="mcp"),
        permission=ToolPermission(), execution_world="local",
    )
    values.update(overrides)
    return ToolGatewayRequest(**values)


class _Cancelled:
    cancelled = True


def test_every_early_exit_leaves_a_receipt_that_names_how_it_ended(tmp_path, workspace):
    denied, audit = _facade(tmp_path, policy=ResourcePolicy())
    with pytest.raises(Forbidden):
        denied.execute(_request())
    assert denied.receipts[-1].terminal == "policy_denied"
    assert denied.receipts[-1].policy_match == "resource:default-deny"

    facade, audit = _facade(tmp_path)
    with pytest.raises(DeadlineExceeded):
        facade.execute(_request(deadline_monotonic=1e-9))
    assert facade.receipts[-1].terminal == "deadline_exceeded"
    with pytest.raises(Cancelled):
        facade.execute(_request(cancellation=_Cancelled()))
    assert facade.receipts[-1].terminal == "cancelled"

    completed = facade.execute(_request())
    assert completed.terminal == "completed" and completed.model == ""
    audit.verify()
    # both facades above share one audit file, so the refused call is first
    assert [record["terminal"] for record in audit.read()] == [
        "policy_denied", "deadline_exceeded", "cancelled", "completed"]
    assert all(record["error_code"] for record in audit.read()[:3])


def test_a_tool_outside_the_family_is_unknown_to_the_typed_registry(tmp_path, workspace):
    facade, _audit_repo = _facade(tmp_path)
    with pytest.raises(InvalidInput):
        facade.execute(_request("run_program", {"program": "python", "args_json": "[]"}))
    assert facade.receipts == ()


def test_the_permission_evaluator_refuses_with_the_decision_attached(monkeypatch):
    monkeypatch.setattr(
        pm, "_rule_lookup",
        lambda tool: {"pattern": tool, "action": pm.DENY, "note": "test"},
    )
    evaluator = PermissionModesEvaluator(policy_names={"read_file": "file_read"})
    with pytest.raises(Forbidden) as caught:
        evaluator.authorize("read_file", ToolScope("owner", source="mcp"), ToolPermission())
    assert caught.value.decision["tool"] == "file_read"
    assert caught.value.decision["source"] == "rule"
    assert caught.value.policy_match == "permission:rule"


def test_the_permission_evaluator_keeps_the_console_exemption_and_no_other(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.PLAN)
    evaluator = PermissionModesEvaluator()
    assert evaluator.authorize("permission_mode", ToolScope("owner", source="repl"),
                               ToolPermission()) == "permission:exempt"
    with pytest.raises(Forbidden):
        evaluator.authorize("permission_mode", ToolScope("owner", source="mcp"), ToolPermission())


def test_the_context_factory_carries_the_scope_source_and_privilege():
    request = _request(scope=ToolScope("owner", ("ws",), frozenset(), source="http",
                                       auth_level="developer"))
    context = default_tool_context(request)
    assert (context.source, context.auth_level, context.principal_id) == ("http", "developer", "owner")
    assert context.correlation_id == "req-1"


@pytest.mark.parametrize("overrides", [{"source": "carrier-pigeon"}, {"auth_level": "root"}])
def test_a_scope_with_an_unknown_source_or_privilege_is_rejected(overrides):
    with pytest.raises(InvalidInput):
        ToolScope("owner", **overrides)


def test_a_receipt_names_a_known_terminal_state():
    assert set(TERMINAL_STATES) == {
        "completed", "failed", "cancelled", "deadline_exceeded", "policy_denied"}
    with pytest.raises(InvalidInput):
        ToolReceipt("r", "t", True, terminal="exploded")


# --- the durable audit ---------------------------------------------------------


def _receipt(index):
    return ToolReceipt(request_id="req-%d" % index, tool_name="program_search",
                       success=True, output="ok", argument_digest="a" * 64,
                       result_digest="b" * 64)


def test_a_full_audit_rotates_and_the_new_chain_names_the_old_one(tmp_path):
    repo = DurableToolAuditRepository(tmp_path / "audit" / "tool-receipts.jsonl",
                                      limits=ToolAuditLimits(max_records=2))
    for index in range(3):
        repo.append(_request(request_id="req-%d" % index), _receipt(index))

    current = repo.read()
    assert len(current) == 1
    assert current[0]["rotated_from"]["records"] == 2
    assert current[0]["rotated_from"]["reason"] == "records"
    assert current[0]["previous_audit_digest"] == ""
    repo.verify()
    rotated = repo.rotated_files()
    assert len(rotated) == 1
    old = DurableToolAuditRepository(rotated[0], limits=ToolAuditLimits(max_records=2))
    old.verify()
    assert current[0]["rotated_from"]["audit_digest"] == old.read()[-1]["audit_digest"]
    assert current[0]["rotated_from"]["path"] == rotated[0].name


def test_rotation_can_be_refused_and_then_a_full_audit_fails_closed(tmp_path):
    repo = DurableToolAuditRepository(tmp_path / "tool-receipts.jsonl",
                                      limits=ToolAuditLimits(max_records=1, rotate=False))
    repo.append(_request(), _receipt(0))
    with pytest.raises(ToolAuditError):
        repo.append(_request(request_id="req-2"), _receipt(1))


def test_audit_records_carry_the_new_fields_redacted(tmp_path):
    repo = DurableToolAuditRepository(tmp_path / "tool-receipts.jsonl")
    receipt = ToolReceipt(request_id="req-1", tool_name="read_file", success=True,
                          output="authorization: Bearer abcdefghijklmnop",
                          terminal="completed", execution_world="local",
                          policy_match="resource:read-only:read_file",
                          evidence={"path": "/ws/notes.txt", "bytes": 3})
    repo.append(_request(scope=ToolScope("owner", source="http", auth_level="developer")),
                receipt)
    record = repo.read()[-1]
    assert record["schema"] == "tool-audit-record-v2"
    assert record["source"] == "http" and record["auth_level"] == "developer"
    assert record["terminal"] == "completed"
    assert record["execution_world"] == "local"
    assert record["policy_match"] == "resource:read-only:read_file"
    assert record["evidence"] == {"path": "/ws/notes.txt", "bytes": 3}
    assert "abcdefghijklmnop" not in json.dumps(record)


# --- the ratchet: no handler reaches a guard on its own ------------------------


LEGACY_HANDLERS = {
    "directory_tree", "file_find", "file_read", "file_read_range",
    "text_search", "script_search", "program_search",
    "file_write", "file_edit", "directory_create", "file_copy", "file_move",
    "file_batch_write", "json_patch", "text_patch", "file_delete",
}


def test_the_legacy_handlers_reach_the_guards_only_through_the_gateway():
    source = pathlib.Path(server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in LEGACY_HANDLERS
    }
    assert len(handlers) == len(LEGACY_HANDLERS)
    for name, node in handlers.items():
        forwarded = False
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Name) and func.id == "_typed_tool":
                forwarded = True
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in {"file_ops", "workbench", "json_patch_tool",
                                          "text_patch_ops"}):
                pytest.fail("%s still calls %s.%s directly" % (name, func.value.id, func.attr))
        assert forwarded, "%s does not forward through the typed gateway" % name


def test_the_family_is_declared_once_and_matches_the_native_route():
    from sonder_runtime.bootstrap import native_mcp

    assert set(typed_tools.TYPED_TOOLS) == set(native_mcp._TYPED_TOOL_NAMES)
    registry = typed_tools.typed_tool_registry()
    assert {item.name for item in registry.list_all()} == set(typed_tools.TYPED_TOOLS)
    for name in typed_tools.READ_ONLY_TOOLS:
        assert pm.risk_of(typed_tools.POLICY_NAMES.get(name, name)) == "safe", name
    for name in typed_tools.MUTATING_TOOLS:
        graded = pm.risk_of(typed_tools.POLICY_NAMES.get(name, name))
        assert graded in pm.UNATTENDED_REFUSED_RISKS, (name, graded)
    legacy = {typed_tools.POLICY_NAMES.get(name, name) for name in typed_tools.TYPED_TOOLS}
    assert legacy == LEGACY_HANDLERS
    # The native surface grades an approved call by the same name the typed
    # evaluator decides on; it derives that map from its alias table rather
    # than importing the declared one, so the two are pinned together here.
    derived = {name: graded for name, graded in native_mcp._GRADED_NAMES.items()
               if name in typed_tools.TYPED_TOOLS}
    assert derived == typed_tools.POLICY_NAMES
