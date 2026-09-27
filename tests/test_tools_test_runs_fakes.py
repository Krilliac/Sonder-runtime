"""Faithful doubles of the developer-tool services for the surface tests.

The inventory and digest doubles stand in for the host-tool-inventory and
diagnostics lanes; the test-run double plans deterministically (its command
digest depends on runner, selector and project, as the real planner's does)
and refuses a selector the real grammar refuses.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.developer_tools_executor import DeveloperToolExecutor
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
from sonder_runtime.application.developer_tools import DeveloperToolServices
from sonder_runtime.application.testing.service import TestRunStatusView
from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
from sonder_runtime.bootstrap.developer_tools import DeveloperToolPermissionEvaluator
from sonder_runtime.bootstrap.typed_tools import POLICY_NAMES, typed_tool_policy, typed_tool_registry
from sonder_runtime.domain.common.errors import InvalidInput, NotFound
from sonder_runtime.domain.testing.runners import runner_from_name
from sonder_runtime.domain.testing.selectors import parse_selector

pytestmark = pytest.mark.unit

CATEGORIES = {"compiler", "build_system", "test_runner", "linter_formatter", "debugger_profiler",
              "package_manager", "runtime", "container_vm", "vcs", "db_client", "media_doc",
              "cloud_cli", "editor_ide", "shell"}


class FakeInventory:
    def __init__(self):
        self.calls = []

    def view(self, *, category=None, name=None, refresh=False, redacted=True):
        self.calls.append((category, name, refresh, redacted))
        if category is not None and category not in CATEGORIES:
            raise InvalidInput("unknown category")
        tools = [{"name": "gcc", "category": "compiler", "version": "13.2.0", "path": "/usr/bin/gcc"},
                 {"name": "pytest", "category": "test_runner", "version": "9.1", "path": "~/.local/bin/pytest"}]
        if category:
            tools = [tool for tool in tools if tool["category"] == category]
        return {"object": "tool_inventory", "tools": tools, "filtered_by": category or name or ""}


@dataclass(frozen=True)
class FakePlan:
    runner: str
    selector: str
    project: str

    def resolved_command(self):
        digest = hashlib.sha256(json.dumps([self.runner, self.selector, self.project]).encode()).hexdigest()
        return {"runner": self.runner, "display_argv": ["python", "-m", "pytest", self.selector],
                "cwd_label": "[WORKSPACE]/" + self.project, "command_digest": digest}


class FakeTestRuns:
    def __init__(self):
        self.planned, self.runs = [], []

    def plan(self, request, context):
        runner = "pytest" if request.runner == "auto" else request.runner
        runner_from_name(runner)
        if request.selector:
            parse_selector(runner, request.selector)
        self.planned.append(request)
        return FakePlan(runner, request.selector, request.project)

    def run(self, request, context, *, wait_seconds):
        plan = self.plan(request, context)
        job = "test-run-" + "a" * 32
        self.runs.append((request, wait_seconds, context.principal_id))
        return TestRunStatusView(job, "running", plan.runner, 0.0,
                                 plan.resolved_command()["command_digest"], ("python",))

    def result(self, job_id, context, *, wait_seconds=0):
        raise NotFound("test run not found")


class Digest:
    def __init__(self, payload):
        self.payload = payload

    def to_wire(self):
        return dict(self.payload)


class FakeDigest:
    def digest_job(self, job_id, context, *, tail_lines=20, max_failure_lines=40, operator=False):
        if operator or not job_id.startswith("test-run-"):
            raise NotFound("no such job")
        return Digest({"source_kind": "job", "final_line": "1 failed", "tail": ["x"] * tail_lines})

    def digest_file(self, path, context, *, tail_lines=20, max_failure_lines=40, max_scan_bytes=4_000_000):
        if path.endswith(".env"):
            raise PermissionError("DIGEST_SOURCE_REJECTED")
        return Digest({"source_kind": "file", "final_line": "ok", "tail": ["line"] * tail_lines})

    def digest_text(self, text, *, label="", tail_lines=20):
        return Digest({"source_kind": "text", "final_line": text.splitlines()[-1] if text else ""})


def services():
    return DeveloperToolServices(inventory=FakeInventory(), test_runs=FakeTestRuns(), digest=FakeDigest())


def facade(tmp_path, developer=None):
    developer = developer if developer is not None else services()
    audit = DurableToolAuditRepository(tmp_path / "audit.jsonl")
    tools = ToolApplicationFacade.compose(
        typed_tool_registry(),
        DeveloperToolExecutor(developer, PackagedToolExecutor(), inventory_wire=lambda view: view),
        policy=typed_tool_policy(), receipts=ReceiptStore(), audit=audit,
        permissions=(DeveloperToolPermissionEvaluator(developer, policy_names=POLICY_NAMES),),
    )
    return tools, audit, developer


def native(tmp_path, calls, *, progressive=False, developer=None):
    from sonder_runtime.bootstrap.native_mcp import run_native_mcp

    tools, audit, developer = facade(tmp_path, developer)
    app = SimpleNamespace(config=None, tools=tools, tool_audit=audit, developer_tools=developer)
    messages = [("initialize", {"protocolVersion": "2.0", "capabilities": {}})] + list(calls)
    stream = io.StringIO("".join(
        json.dumps({"jsonrpc": "2.0", "id": index, "method": method, "params": params}) + "\n"
        for index, (method, params) in enumerate(messages)))
    output = io.StringIO()
    run_native_mcp(app, input_stream=stream, output_stream=output, progressive_tools=progressive)
    return [json.loads(row) for row in output.getvalue().splitlines()][1:], audit, developer


def test_the_plan_double_refuses_what_the_grammar_refuses():
    with pytest.raises(InvalidInput):
        FakeTestRuns().plan(SimpleNamespace(runner="pytest", selector="--x", project="."), None)
