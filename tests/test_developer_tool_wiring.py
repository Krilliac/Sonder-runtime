"""End-to-end wiring of the developer tools in the real application graph.

The composed path needs the host-tool-inventory and diagnostics packages; a
build without them must still compose and report the tools unavailable.
Both branches are asserted: whichever one this checkout is on runs.
"""
from __future__ import annotations

import importlib.util
import io
import json
import uuid

import pytest

import permission_modes as pm
from sonder_runtime.application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.bootstrap.native_mcp import run_native_mcp
from sonder_runtime.platform import paths as runtime_paths

pytestmark = pytest.mark.integration

DEVELOPER = {"tool_inventory", "test_run", "test_run_result", "output_digest"}
_COMPOSABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("sonder_runtime.bootstrap.host_tools", "sonder_runtime.bootstrap.diagnostics")
)


@pytest.fixture
def application(tmp_path, monkeypatch):
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    try:
        yield bootstrap_app.build_application()
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)
        if _COMPOSABLE:
            from sonder_runtime.bootstrap.host_tools import uninstall_agent_brief_summary

            uninstall_agent_brief_summary()


def _typed(application, tool, arguments):
    descriptor = application.tools.graph.registry.get(tool)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    return application.tools.execute(ToolGatewayRequest(
        request_id=uuid.uuid4().hex, tool_name=tool, arguments=arguments,
        scope=ToolScope("local-owner", (), effects, source="repl"),
        permission=ToolPermission(effects), execution_world="local",
    ))


def _native_list(application):
    stream = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2.0", "capabilities": {}}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
    output = io.StringIO()
    run_native_mcp(application, input_stream=stream, output_stream=output)
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    return {item["name"] for item in rows[1]["result"]["tools"]}


def test_the_typed_registry_and_native_catalog_carry_the_tools(application):
    registered = {item.name for item in application.tools.graph.registry.list_all()}
    assert DEVELOPER <= registered
    assert DEVELOPER <= _native_list(application)


@pytest.mark.skipif(_COMPOSABLE, reason="this build composes the developer tools")
def test_without_the_inventory_and_digest_packages_the_tools_report_unavailable(application):
    assert application.developer_tools is None
    receipt = _typed(application, "tool_inventory", {})
    assert not receipt.success
    assert json.loads(receipt.output)["error_code"] == "DEVELOPER_TOOLS_UNAVAILABLE"


@pytest.mark.skipif(not _COMPOSABLE, reason="needs the host_tools and diagnostics packages")
def test_the_composed_services_serve_every_surface(application):
    from sonder_runtime.application.diagnostics.service import OutputDigestService
    from sonder_runtime.application.host_tools.service import HostToolInventoryService
    from sonder_runtime.application.testing.service import TestRunService
    from sonder_runtime.interfaces.http.facades.host_tools import dispatch_tool_inventory
    from sonder_runtime.interfaces.repl.facades.developer_tools import render_tools_command
    from sonder_runtime.platform import environment_probe

    services = application.developer_tools
    assert isinstance(services.inventory, HostToolInventoryService)
    assert isinstance(services.test_runs, TestRunService)
    assert isinstance(services.digest, OutputDigestService)

    receipt = _typed(application, "tool_inventory", {"category": "vcs"})
    assert receipt.success, receipt.error
    body = json.loads(receipt.output)
    assert body["object"] == "tool_inventory"

    # The capability summary joins the agent brief once a snapshot exists.
    assert services.inventory.cached() is not None
    brief = environment_probe.agent_brief()
    summary = services.inventory.capability_summary()
    if summary:
        assert "capabilities:" in brief

    status, payload = dispatch_tool_inventory(lambda: services.inventory, {})
    assert status == 200 and payload["object"] == "tool_inventory"
    assert isinstance(render_tools_command(services, ""), str)
