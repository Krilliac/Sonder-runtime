"""Scripted model, real filesystem/process coding acceptance in a disposable Git repo."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from sonder_runtime.adapters.lane_tests import (
    LaneTestCatalog,
    LaneTestExecutor,
    lane_test_descriptor,
)
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry
from sonder_runtime.application.tools.facade import ToolApplicationFacade
from sonder_runtime.application.tools.resource_policy import (
    ResourcePolicy,
    PolicyRule,
    Decision,
)
from sonder_runtime.bootstrap.typed_tools import typed_tool_registry, typed_tool_policy


class ScriptedModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse(
            next(self.replies), "scripted-acceptance-model", "code", tokens_out=50
        )


def tool(name, **args):
    return json.dumps({"tool": name, "arguments": args})


@pytest.fixture
def coding(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def total(values):\n    return sum(values) + 1\n", encoding="utf-8"
    )
    (repo / "test_calc.py").write_text(
        "from calc import total\n\ndef test_empty():\n    assert total([]) == 0\n\ndef test_sum():\n    assert total([2, 3]) == 5\n",
        encoding="utf-8",
    )
    (repo / "sleep_test.py").write_text(
        'import time\nprint("started", flush=True)\ntime.sleep(60)\n', encoding="utf-8"
    )
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    for args in (
        ["init", "-q"],
        ["add", "calc.py", "test_calc.py", "sleep_test.py", ".gitignore"],
        [
            "-c",
            "user.name=Acceptance",
            "-c",
            "user.email=acceptance@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, timeout=10
        )
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(repo))
    catalog_path = tmp_path / "lane-tests.json"
    catalog_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "name": "unit",
                        "workspace_root": str(repo),
                        "argv": [
                            sys.executable,
                            "-m",
                            "pytest",
                            "-q",
                            "-p",
                            "no:cacheprovider",
                            "test_calc.py",
                        ],
                        "timeout_seconds": 20,
                    },
                    {
                        "name": "slow",
                        "workspace_root": str(repo),
                        "argv": [sys.executable, "sleep_test.py"],
                        "timeout_seconds": 20,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = LaneTestCatalog.load(catalog_path)
    jobs = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(
        jobs, process_cleanup=ProcessTreeSupervisor(), max_concurrent_processes=1
    )
    executor = LaneTestExecutor(catalog, provider)
    registry = InMemoryToolRegistry(
        [*typed_tool_registry().list_all(), lane_test_descriptor(catalog)]
    )
    policy = ResourcePolicy(
        [
            *typed_tool_policy().rules,
            PolicyRule("explicit-fixture-tests", Decision.ALLOW, tool="run_tests"),
        ]
    )
    from sonder_runtime.bootstrap.lane_tests import _test_context

    facade = ToolApplicationFacade.compose(
        registry, executor, policy=policy, context_factory=_test_context
    )
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    context = local_owner_context(
        correlation_id="coding-fixture", workspace_roots=(repo,)
    )
    return repo, catalog_path, store, sessions, jobs, provider, facade, context


def make_service(coding, replies):
    _, _, store, sessions, _, _, facade, _ = coding
    model = ScriptedModel(replies)
    service = AgentLaneService(
        store,
        sessions,
        model,
        facade,
        auto_start=False,
        allowed_tools=("read_file", "edit_file", "run_tests"),
    )
    return service, model


def test_scripted_model_real_edit_failing_test_repair_passing_test_and_diff(coding):
    repo, _, store, sessions, jobs, _, facade, context = coding
    service, model = make_service(
        coding,
        [
            tool("read_file", path="calc.py"),
            tool("run_tests", target="unit"),
            tool(
                "edit_file",
                path="calc.py",
                old="return sum(values) + 1",
                new="return sum(values)",
            ),
            tool("run_tests", target="unit"),
            "Removed the extra 1. Both targeted tests now pass.",
        ],
    )
    lane = service.spawn(
        command_id="code",
        parent_session_id="parent",
        task="Fix total and verify its tests",
        workspace_root=str(repo),
        context=context,
        max_steps=8,
    )["lane"]["id"]
    service.run_pending(lane, context)
    state = service.inspect(lane, context)
    assert state["lane"]["status"] == "completed", state
    assert (
        repo / "calc.py"
    ).read_text() == "def total(values):\n    return sum(values)\n"
    results = [
        json.loads(r.output) for r in facade.receipts if r.tool_name == "run_tests"
    ]
    assert [r["exit_code"] for r in results] == [1, 0]
    assert "2 failed" in results[0]["output"] and "2 passed" in results[1]["output"]
    assert all(jobs.poll(r["job_id"]).is_terminal for r in results)
    diff = subprocess.run(
        ["git", "diff", "--", "calc.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    assert "-    return sum(values) + 1" in diff and "+    return sum(values)" in diff
    (repo.parent / "review.diff").write_text(diff, encoding="utf-8")
    reopened = AgentLaneService(
        store,
        sessions,
        model,
        facade,
        auto_start=False,
        allowed_tools=("read_file", "edit_file", "run_tests"),
    )
    assert (
        reopened.inspect(lane, context)["lane"]["session_id"]
        == state["lane"]["session_id"]
    )
    reports = reopened.reports("parent", context)["reports"]
    assert len(reports) == 1 and reports[0]["artifacts"] == [str(repo / "calc.py")]
    assert any("2 failed" in str(request.history) for request in model.requests[2:])
    assert '"unit"' in model.requests[0].system


def test_process_restart_consumes_known_test_request_once(coding):
    repo, _, store, sessions, jobs, _, facade, context = coding
    service, _ = make_service(coding, [])
    lane = service.spawn(
        command_id="restart",
        parent_session_id="parent",
        task="Run unit tests",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    script = """
import os, sys
from pathlib import Path
from types import SimpleNamespace
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
class Model:
    def generate(self, request, context):
        return ModelResponse('{"tool":"run_tests","arguments":{"target":"unit"}}', 'scripted', 'code', tokens_out=10)
root = Path(sys.argv[1])
sessions = SQLiteSessionRepository(root/'sessions.db')
store = SQLiteAgentLaneStore(root/'fleet.db', sessions)
flush = store.flush
def crash_after_response():
    with store.transaction() as tx:
        known = bool(tx.lane(sys.argv[2]).get('pending_response'))
    if known:
        os._exit(22)
    flush()
store.flush = crash_after_response
facade = SimpleNamespace(graph=SimpleNamespace(registry=SimpleNamespace(list_all=lambda: [], get=lambda name: SimpleNamespace(input_schema={}))))
service = AgentLaneService(store, sessions, Model(), facade, auto_start=False, allowed_tools=('run_tests', 'read_file', 'edit_file'))
service.run_pending(sys.argv[2], local_owner_context(correlation_id='restart-child', workspace_roots=(root/'repo',)))
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(repo.parent), lane], timeout=15
    )
    assert child.returncode == 22
    reopened, model = make_service(
        coding, ["The recorded unit test run failed; no repair was requested."]
    )
    state = reopened.inspect(lane, context)
    assert state["lane"]["status"] == "interrupted"
    assert not jobs.list(include_terminal=True, limit=10)
    reopened.control(lane, "resume", command_id="resume-known", context=context)
    reopened.run_pending(lane, context)
    final = reopened.inspect(lane, context)
    assert final["lane"]["status"] == "completed", final
    assert len(model.requests) == 1
    assert len(jobs.list(include_terminal=True, limit=10)) == 1
    assert len(reopened.reports("parent", context)["reports"]) == 1
    events = sessions.read_range(final["lane"]["session_id"])
    assert sum(e.event_type == "model.requested" for e in events) == 2
    assert sum(e.event_type == "tool.requested" for e in events) == 1


def test_live_test_process_cancellation_records_proven_cleanup(coding):
    repo, _, _, _, jobs, _, facade, context = coding
    service, _ = make_service(
        coding, [tool("run_tests", target="slow"), "should not execute"]
    )
    lane = service.spawn(
        command_id="slow",
        parent_session_id="parent",
        task="Run configured slow test",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    worker = threading.Thread(target=service.run_pending, args=(lane, context))
    worker.start()
    end = time.monotonic() + 10
    while not jobs.list(include_terminal=True, limit=10) and time.monotonic() < end:
        time.sleep(0.02)
    assert jobs.list(include_terminal=True, limit=10)
    service.control(lane, "cancel", command_id="cancel-live", context=context)
    worker.join(10)
    assert not worker.is_alive()
    result = json.loads(
        next(r.output for r in facade.receipts if r.tool_name == "run_tests")
    )
    assert result["cancelled"] and result["cleanup_completed"]
    assert service.inspect(lane, context)["lane"]["status"] == "cancelled"
    assert jobs.poll(result["job_id"]).status.value == "cancelled"


@pytest.mark.parametrize("success", [False, True])
def test_tool_gateway_retains_completed_effect_truth_after_cancellation(success):
    from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
    from sonder_runtime.application.ports.tool_registry import ToolDescriptor
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolScope,
        ToolPermission,
    )

    class Token:
        cancelled = False

    token = Token()

    class Executor:
        calls = 0

        def execute(self, *args):
            self.calls += 1
            token.cancelled = True
            return ToolExecutionResult(
                "effect",
                success,
                '{"cleanup_completed":true}',
                error_code="" if success else "TEST_CANCELLED",
            )

    executor = Executor()
    facade = ToolApplicationFacade.compose(
        InMemoryToolRegistry([ToolDescriptor("effect")]),
        executor,
        policy=ResourcePolicy([PolicyRule("approved", Decision.ALLOW, tool="effect")]),
    )
    request = ToolGatewayRequest(
        "one", "effect", {}, ToolScope("owner"), ToolPermission(), cancellation=token
    )
    receipt = facade.execute(request)
    assert receipt.output == '{"cleanup_completed":true}' and receipt.error_code == (
        "" if success else "TEST_CANCELLED"
    )
    assert receipt.terminal == ("completed" if success else "cancelled")
    from sonder_runtime.application.errors import Cancelled

    with pytest.raises(Cancelled):
        facade.execute(request)
    assert executor.calls == 1


def test_test_execution_requires_explicit_lane_grant(coding):
    repo, _, store, sessions, jobs, _, facade, context = coding
    model = ScriptedModel([tool("run_tests", target="unit")])
    service = AgentLaneService(store, sessions, model, facade, auto_start=False)
    lane = service.spawn(
        command_id="denied",
        parent_session_id="parent",
        task="test",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane, context)
    assert service.inspect(lane, context)["lane"]["status"] == "failed"
    assert not jobs.list(include_terminal=True, limit=10)


def test_provider_deadline_cleanup_race_retains_cancelled_receipt(coding, monkeypatch):
    repo, _, _, _, _, provider, facade, context = coding
    original_wait = provider.wait

    def deadline_before_wait(job_id, **kwargs):
        provider.cancel(job_id, reason="configured test deadline")
        return original_wait(job_id, **kwargs)

    monkeypatch.setattr(provider, "wait", deadline_before_wait)
    service, _ = make_service(
        coding, [tool("run_tests", target="slow"), "Test cancelled."]
    )
    lane = service.spawn(
        command_id="deadline-race",
        parent_session_id="parent",
        task="run",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane, context)
    receipts = [r for r in facade.receipts if r.tool_name == "run_tests"]
    assert receipts and receipts[0].terminal == "cancelled"
    body = json.loads(receipts[0].output)
    assert body["cancelled"] and body["cleanup_completed"]


def test_catalog_is_immutable_and_outside_model_workspace(coding):
    repo, path, _, _, _, _, _, _ = coding
    raw = json.loads(path.read_text())
    inside = repo / "targets.json"
    inside.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="outside"):
        LaneTestCatalog.load(inside)
    catalog = LaneTestCatalog.load(path)
    path.write_text(json.dumps({"targets": []}))
    with pytest.raises(PermissionError, match="changed"):
        catalog.require_current()


def test_catalog_cannot_live_in_another_model_writable_root(coding, monkeypatch):
    repo, path, _, _, _, _, _, _ = coding
    other = repo.parent / "other-root"
    other.mkdir()
    catalog = other / "catalog.json"
    catalog.write_bytes(path.read_bytes())
    monkeypatch.setenv("SONDER_FILE_ROOTS", os.pathsep.join((str(repo), str(other))))
    with pytest.raises(ValueError, match="writable"):
        LaneTestCatalog.load(catalog)


def test_missing_native_containment_refuses_before_process_launch(coding, monkeypatch):
    from types import SimpleNamespace

    repo, _, _, _, _, provider, _, context = coding
    launched = []
    monkeypatch.setattr(
        provider, "_memory_limiter", SimpleNamespace(apply=lambda *args: None)
    )
    monkeypatch.setattr(
        provider, "_launcher", lambda *args, **kwargs: launched.append(True)
    )
    service, _ = make_service(coding, [tool("run_tests", target="unit")])
    lane = service.spawn(
        command_id="no-containment",
        parent_session_id="parent",
        task="run",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane, context)
    assert service.inspect(lane, context)["lane"]["status"] == "awaiting_input"
    assert not launched


def test_real_test_process_receives_no_ambient_credentials_or_controls(
    coding, monkeypatch
):
    repo, path, store, sessions, jobs, provider, _, context = coding
    names = [
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "custom_PASSWORD",
        "SSH_AUTH_SOCK",
        "HTTPS_PROXY",
        "SONDER_PERMISSION_MODE",
        "SONDER_FILE_BYPASS",
        "PYTHONPATH",
    ]
    for name in names:
        monkeypatch.setenv(name, "ambient-value-must-not-inherit")
    probe = repo / "env_probe.py"
    probe.write_text(
        "import os, json\nprint(json.dumps(sorted(os.environ)), flush=True)\n",
        encoding="utf-8",
    )
    body = json.loads(path.read_text())
    body["targets"] = [
        dict(
            name="env", workspace_root=str(repo), argv=[sys.executable, "env_probe.py"]
        )
    ]
    path.write_text(json.dumps(body), encoding="utf-8")
    catalog = LaneTestCatalog.load(path)
    facade = ToolApplicationFacade.compose(
        InMemoryToolRegistry([lane_test_descriptor(catalog)]),
        LaneTestExecutor(catalog, provider),
        policy=ResourcePolicy(
            [PolicyRule("fixture", Decision.ALLOW, tool="run_tests")]
        ),
    )
    service = AgentLaneService(
        store,
        sessions,
        ScriptedModel([tool("run_tests", target="env"), "done"]),
        facade,
        auto_start=False,
        allowed_tools=("run_tests",),
    )
    lane = service.spawn(
        command_id="env",
        parent_session_id="parent",
        task="probe",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane, context)
    result = json.loads(
        next(r.output for r in facade.receipts if r.tool_name == "run_tests")
    )
    assert result["exit_code"] == 0
    inherited = {name.upper() for name in json.loads(result["output"])}
    assert not inherited.intersection(name.upper() for name in names)
    assert not inherited.intersection({"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"})
    from sonder_runtime.adapters.lane_tests import _minimal_environment
    assert inherited == {name.upper() for name, _ in _minimal_environment(sys.executable)}
    if sys.platform == "win32":
        assert "SYSTEMROOT" in inherited


@pytest.mark.parametrize("admitted", [False, True])
def test_supported_composition_preserves_operator_execution_gate(
    coding, monkeypatch, admitted
):
    from sonder_runtime.bootstrap.lane_tests import compose_lane_test_tools
    from sonder_runtime.adapters.security.permission_evaluator import (
        PermissionModesEvaluator,
    )
    from sonder_runtime.application.errors import Forbidden
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolScope,
        ToolPermission,
    )

    repo, path, _, _, jobs, provider, base, context = coding
    # Build from the ordinary file facade so run_tests is registered only once.
    files = ToolApplicationFacade.compose(
        typed_tool_registry(), policy=typed_tool_policy()
    )
    configured = compose_lane_test_tools(
        files, LaneTestCatalog.load(path), provider, audit=None
    )

    def refuse(self, name, scope, **kwargs):
        assert self._policy_names[name] == "workspace_run"
        assert kwargs["arguments"]["configured_target"]["argv"] == list(
            LaneTestCatalog.load(path).targets["unit"].argv
        )
        if admitted:
            return "permission:explicit-test-fixture"
        raise Forbidden("execution denied by operator")

    monkeypatch.setattr(PermissionModesEvaluator, "_decide", refuse)
    effects = frozenset({"read_files", "write_files", "execute"})
    request = ToolGatewayRequest(
        "denied-call",
        "run_tests",
        {"target": "unit"},
        ToolScope("owner", (str(repo),), effects, source="worker"),
        ToolPermission(effects),
    )
    if admitted:
        receipt = configured.execute(request)
        assert json.loads(receipt.output)["exit_code"] == 1
        assert len(jobs.list(include_terminal=True, limit=10)) == 1
    else:
        with pytest.raises(Forbidden):
            configured.execute(request)
        assert not jobs.list(include_terminal=True, limit=10)


@pytest.mark.parametrize("revocation", ["catalog", "parent_grant"])
def test_catalog_revocation_cancels_running_job(coding, revocation):
    repo, path, _, _, jobs, _, facade, context = coding
    service, _ = make_service(
        coding, [tool("run_tests", target="slow"), "No tests passed"]
    )
    lane = service.spawn(
        command_id="revoke",
        parent_session_id="parent",
        task="run",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    worker = threading.Thread(target=service.run_pending, args=(lane, context))
    worker.start()
    end = time.monotonic() + 10
    while not jobs.list(include_terminal=True, limit=10) and time.monotonic() < end:
        time.sleep(0.02)
    assert jobs.list(include_terminal=True, limit=10)
    if revocation == "catalog":
        path.write_text(json.dumps({"targets": []}))
    else:

        def reject(lane, context):
            raise PermissionError("parent grant revoked")

        service.authorize_grant = reject
    worker.join(10)
    assert not worker.is_alive()
    receipt = next(r for r in facade.receipts if r.tool_name == "run_tests")
    result = json.loads(receipt.output)
    assert result["cancelled"] and result["cleanup_completed"]


def test_delegated_independent_certificate_after_real_scripted_repair(coding):
    from sonder_runtime.bootstrap.delegated_verification import (
        compose_delegated_verification,
    )

    repo, catalog_path, store, sessions, jobs, provider, facade, context = coding
    service, model = make_service(
        coding,
        [
            tool("run_tests", target="unit"),
            tool(
                "edit_file",
                path="calc.py",
                old="return sum(values) + 1",
                new="return sum(values)",
            ),
            tool("run_tests", target="unit"),
            "Repaired",
        ],
    )
    parent = service.open_model_parent(context)
    lane_id = service.spawn(
        command_id="coding",
        parent_session_id=parent["parent_session_id"],
        task="Repair and test total",
        workspace_root=str(repo),
        context=context,
        max_steps=8,
    )["lane"]["id"]
    service.run_pending(lane_id, context)
    assert service.inspect(lane_id, context)["lane"]["status"] == "completed"
    verifier = compose_delegated_verification(
        service, provider, catalog_path, targets={str(repo): "unit"}
    )
    prepared = verifier.prepare(
        parent["parent_session_id"],
        command_id="independent-check",
        context=context,
        bound_parent_revision=parent["revision"],
    )
    approvals = []

    def approve(bundle, ctx):
        approvals.append(bundle.approval_payload())
        return "explicit-independent-operator-approval"

    result = verifier.execute_prepared(prepared, context=context, approve=approve)
    assert result["state"] == "certified", result
    assert len(approvals) == 1
    assert len(result["certificate"]["cleanup_proofs"]) == 1
    assert verifier.validate(
        parent["parent_session_id"],
        prepared.verification_id,
        context=context,
        bound_parent_revision=parent["revision"],
    ).valid
    assert provider.poll(result["job_ids"][0]).result["exit_code"] == 0
    diff = subprocess.run(
        ["git", "diff", "--", "calc.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "+    return sum(values)" in diff.stdout
    (repo / "untracked_source.py").write_text("changed = True\n")
    assert not verifier.validate(
        parent["parent_session_id"],
        prepared.verification_id,
        context=context,
        bound_parent_revision=parent["revision"],
    ).valid


def test_test_command_mutating_source_cannot_certify(coding):
    from sonder_runtime.bootstrap.delegated_verification import (
        compose_delegated_verification,
    )

    repo, catalog_path, _, _, _, provider, _, context = coding
    service, _ = make_service(coding, ["Finished"])
    parent = service.open_model_parent(context)
    lane_id = service.spawn(
        command_id="child",
        parent_session_id=parent["parent_session_id"],
        task="Inspect only",
        workspace_root=str(repo),
        context=context,
    )["lane"]["id"]
    service.run_pending(lane_id, context)
    catalog_path.write_text(
        json.dumps(
            {
                "targets": [
                    {
                        "name": "mutating",
                        "workspace_root": str(repo),
                        "argv": [
                            sys.executable,
                            "-c",
                            'from pathlib import Path; Path("calc.py").write_text("changed = True\\n")',
                        ],
                    }
                ]
            }
        )
    )
    verifier = compose_delegated_verification(service, provider, catalog_path)
    prepared = verifier.prepare(
        parent["parent_session_id"],
        command_id="verify-mutating",
        context=context,
        bound_parent_revision=1,
    )
    result = verifier.execute_prepared(
        prepared, context=context, approve=lambda *args: "independent-approval"
    )
    assert result["state"] == "failed" and result["certificate"] is None
    assert provider.cleanup_proof(result["job_ids"][0])["exit_code"] == 0
