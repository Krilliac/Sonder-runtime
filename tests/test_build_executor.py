"""BuildToolExecutor: error mapping, unavailability, fallback, wire bounds.

This module also holds the faithful doubles of the build services the other
build surface tests share (``FakeBuildServices``, ``compose_facade``,
``gateway_call``). The doubles plan deterministically: a plan's command
digest depends on exactly the fields the real planner digests (action,
target, config, platform, preset, file, network), and they refuse what the
real planner refuses at this seam (an unknown target, a utility target).

The ``*RequestDouble`` dataclasses carry the spec's request fields for the
fake plans; the executor itself builds the real request types from
``application.build.ports`` and ``application.build.fix_ports``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.build.executor import (
    BUILD_TOOLS_UNAVAILABLE,
    BUILD_TYPED_TOOLS,
    KNOWN_ERROR_CODES,
    MAX_WIRE_BYTES,
    BuildToolExecutor,
    fit_payload,
)
from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
from sonder_runtime.application.ports.tool_registry import ToolCall
from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.bootstrap.build_tools import BuildFixGrantRegistry, build_permission_resolvers
from sonder_runtime.bootstrap.developer_tools import DeveloperToolPermissionEvaluator
from sonder_runtime.bootstrap.typed_tools import POLICY_NAMES, typed_tool_policy, typed_tool_registry
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
)
from sonder_runtime.domain.tools.descriptors import ExecutionClass

pytestmark = pytest.mark.unit

# --- port doubles ------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRequestDouble:
    project: str = "."
    build_dir: str = ""
    preset: str = ""
    refresh: bool = False


@dataclass(frozen=True)
class JobRequestDouble:
    project: str = "."
    build_dir: str = ""
    action: str = "build"
    target: str = ""
    config: str = ""
    platform: str = ""
    preset: str = ""
    build_preset: str = ""
    file: str = ""
    generator: str = ""
    profile: str = ""
    jobs: int | None = None
    timeout_seconds: int | None = None
    allow_network: bool = False


@dataclass(frozen=True)
class FixRequestDouble:
    project: str = "."
    build_dir: str = ""
    target: str = ""
    config: str = ""
    platform: str = ""
    focus_file: str = ""
    attempts: int = 4
    apply: bool = True
    revert_after: bool = False
    editable_globs: tuple = ()
    timeout_seconds: int | None = None
    verify_dependents: bool = False
    allow_network: bool = False


# --- service doubles -----------------------------------------------------------------------


def coded(cls, code: str, message: str = "refused"):
    error = cls(message)
    error.code = code
    return error


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class FakeModels:
    def __init__(self):
        self.views = []
        self.summaries: dict[str, str] = {}
        self.summary_calls = []

    def view(self, request, context, *, detail="summary", target="", max_items=100):
        self.views.append((request, context.principal_id, detail))
        if request.project == "missing":
            raise coded(InvalidInput, "BUILD_TREE_MISSING", "configure first")
        return {"object": "build_model", "project": "sparklite", "detail": detail,
                "targets": [{"name": "game"}, {"name": "core"}]}

    def cached_summary(self, principal_id, project_label=""):
        self.summary_calls.append((principal_id, project_label))
        return self.summaries.get(principal_id, "")


@dataclass(frozen=True)
class FakeJobPlan:
    action: str
    target: str
    config: str
    platform: str
    preset: str
    file: str
    network: str
    build_dir: str = ""
    template_id: str = "cmake.build"
    world: str = "host"

    def resolved_command(self):
        return {"action": self.action, "system": "cmake", "template_id": self.template_id,
                "display_argv": ["cmake", "--build", "[BUILD]", "--target", self.target],
                "project": "sparklite", "target": self.target, "config": self.config,
                "platform": self.platform, "world": self.world, "network": self.network,
                "command_digest": _digest(self.action, self.target, self.config, self.platform,
                                          self.preset, self.file, self.network)}


class FakeJobs:
    TARGETS = {"", "all", "game", "core", "shadergen"}
    UTILITY = {"deploy"}

    def __init__(self):
        self.planned, self.runs, self.cancelled = [], [], []
        self.owners: dict[str, str] = {}

    def plan(self, request, context, *, lease=None):
        if request.target in self.UTILITY:
            raise coded(Forbidden, "UTILITY_TARGET_REFUSED", "utility target")
        if request.target not in self.TARGETS:
            raise coded(InvalidInput, "UNKNOWN_TARGET", "no such target")
        self.planned.append(request)
        return FakeJobPlan(request.action, request.target, request.config, request.platform,
                           request.preset, request.file,
                           "allowed" if request.allow_network else "enforced_off")

    def run(self, request, context, *, wait_seconds, **kwargs):
        plan = self.plan(request, context)
        job_id = "build-job-" + uuid.uuid4().hex
        self.owners[job_id] = context.principal_id
        self.runs.append((request, wait_seconds, context.principal_id))
        return {"object": "build_job_status", "job_id": job_id, "status": "running",
                "action": plan.action, "command_digest": plan.resolved_command()["command_digest"]}

    def _owned(self, job_id, context):
        if self.owners.get(job_id) != context.principal_id:
            raise coded(NotFound, "JOB_NOT_FOUND", "build job not found")

    def result(self, job_id, context, *, wait_seconds=0):
        self._owned(job_id, context)
        return {"object": "build_job_report", "job_id": job_id, "status": "failed",
                "first_errors": ["src/core/math.cpp:3:10: error: 'lenght' was not declared"],
                "world": "host", "network": "enforced_off", "isolation_truth": "unverified"}

    def cancel(self, job_id, context, *, reason="cancelled"):
        self._owned(job_id, context)
        self.cancelled.append(job_id)
        return {"object": "build_job_status", "job_id": job_id, "status": "cancelled",
                "cleanup_proven": True}


class FakeScope:
    """``EditScope`` double: project sources only; tools and scripts excluded."""

    SUFFIXES = (".cpp", ".cc", ".h", ".hpp", ".c", ".inl")

    def __init__(self, excluded=("tools/shadergen.cpp",)):
        self.excluded = frozenset(excluded)

    def allows(self, rel):
        name = rel.rsplit("/", 1)[-1]
        if rel in self.excluded:
            return False, "BUILD_TIME_TOOL_SOURCE"
        if name == "CMakeLists.txt" or name.endswith(".cmake"):
            return False, "NEEDS_BUILD_SCRIPT_CHANGE"
        if name.startswith(".env") or not name.endswith(self.SUFFIXES):
            return False, "OUT_OF_SCOPE_FILE"
        return True, ""


@dataclass
class FakeFixPlan:
    grant_spec: SimpleNamespace
    scope: FakeScope
    plan_digest: str
    request: FixRequestDouble | None = None

    def resolved_command(self):
        return {"template_id": "cmake.build", "target": self.grant_spec.target,
                "config": self.grant_spec.config, "network": self.grant_spec.network,
                "plan_digest": self.plan_digest, "attempts": getattr(self.request, "attempts", 4)}


class FakeFix:
    """``BuildFixService`` double with the real service's signatures (checked by
    ``test_service_doubles_match_the_real_signatures``) and its grant protocol:
    ``start(plan=)`` issues the job's grant from the approval the executor
    claimed, and the grant is revoked when the job ends."""

    def __init__(self, project_root="", build_dir="", *, clock=time.time, grants=None):
        self.project_root = str(project_root)
        self.build_dir = str(build_dir)
        self.clock = clock
        self.grants = grants
        self.planned, self.started, self.restored = [], [], []
        self.active: set[str] = set()
        self.owners: dict[str, str] = {}

    def plan(self, request, context):
        from sonder_runtime.application.build.grants import BuildFixGrantSpec

        if request.target not in FakeJobs.TARGETS or not request.target:
            raise coded(InvalidInput, "UNKNOWN_TARGET", "no such target")
        self.planned.append(request)
        network = "allowed" if request.allow_network else "enforced_off"
        scope = FakeScope()
        spec = BuildFixGrantSpec(
            project_root=self.project_root or "/nonexistent", build_dir=self.build_dir,
            target=request.target, config=request.config, platform=request.platform,
            template_ids=("cmake.build", "ninja.compile_one"), world="host", network=network,
            scope_digest="5" * 64, max_files=6, max_changed_lines=400,
            expires_at=self.clock() + 3600, allow_network=bool(request.allow_network), scope=scope,
        )
        digest = _digest(request.target, request.config, request.platform, request.attempts,
                         request.apply, request.revert_after, network)
        return FakeFixPlan(spec, scope, digest, request)

    def start(self, request, context, *, plan=None):
        from sonder_runtime.application.build.grants import grant_carrier

        if plan is not None and plan.request != request:
            raise InvalidInput("the approved fix plan does not match this request")
        plan = plan if plan is not None else self.plan(request, context)
        job_id = "build-fix-" + uuid.uuid4().hex
        grant = None
        if self.grants is not None:
            grant = self.grants.issue(plan.grant_spec, principal_id=context.principal_id,
                                      job_id=job_id, plan_digest=plan.plan_digest)
        self.started.append((request, context.principal_id, grant_carrier(grant.token) if grant else ""))
        self.active.add(job_id)
        self.owners[job_id] = context.principal_id
        return job_id

    def finish(self, job_id):
        self.active.discard(job_id)
        if self.grants is not None:
            self.grants.revoke(job_id)

    def _owned(self, job_id, context):
        if self.owners.get(job_id) != context.principal_id:
            raise coded(NotFound, "JOB_NOT_FOUND", "build fix not found")

    def result(self, job_id, context, *, wait_seconds=0):
        self._owned(job_id, context)
        status = "running" if job_id in self.active else "fixed"
        return {"object": "build_fix_report", "job_id": job_id, "status": status,
                "stop_reason": "" if status == "running" else "FIXED",
                "verification_scope": "target"}

    def cancel(self, job_id, context, *, reason="cancelled"):
        self._owned(job_id, context)
        self.finish(job_id)
        return {"object": "build_fix_status", "job_id": job_id, "status": "cancelled"}

    def restore(self, job_id, context, *, files=()):
        self._owned(job_id, context)
        if "changed.cpp" in files:
            raise coded(Conflict, "RESTORE_CONFLICT", "file changed since the fix")
        self.restored.append((job_id, tuple(files)))
        return {"object": "build_fix_restore", "job_id": job_id, "restored": list(files)}


def fake_services(project_root="", build_dir="", *, fix=True, clock=time.time):
    return SimpleNamespace(model=FakeModels(), jobs=FakeJobs(),
                           fix=FakeFix(project_root, build_dir, clock=clock) if fix else None)


def compose_facade(tmp_path, services, *, grants=None, network_decider=None, fallback=None,
                   developer=None):
    grants = grants if grants is not None else BuildFixGrantRegistry()
    if services is not None and getattr(services, "fix", None) is not None:
        services.fix.grants = grants  # the service issues and revokes the job grants
    audit = DurableToolAuditRepository(tmp_path / "audit.jsonl")
    evaluator = DeveloperToolPermissionEvaluator(
        developer, policy_names=POLICY_NAMES,
        resolvers=build_permission_resolvers(services, grants=grants,
                                             network_decider=network_decider),
        grant_authorities=(grants,),
    )
    tools = ToolApplicationFacade.compose(
        typed_tool_registry(),
        BuildToolExecutor(services, fallback or PackagedToolExecutor(), grants=grants),
        policy=typed_tool_policy(), receipts=ReceiptStore(), audit=audit,
        permissions=(evaluator,),
    )
    return tools, audit, grants


def gateway_call(tools, name, arguments, *, principal="owner", source="mcp", gate="gateway",
                 token=None, request_id=None, roots=()):
    descriptor = tools.graph.registry.get(name)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    request = ToolGatewayRequest(
        request_id=request_id or ("req-" + uuid.uuid4().hex),
        tool_name=name, arguments=arguments,
        scope=ToolScope(principal_id=principal, workspace_roots=tuple(roots),
                        allowed_effects=effects, source=source, gate=gate),
        permission=ToolPermission(effects), approval_token=token, execution_world="local",
    )
    return tools.execute(request)


def output(receipt) -> dict:
    return json.loads(receipt.output)


# --- executor tests --------------------------------------------------------------------------


def _execute(executor, name, arguments, principal="owner"):
    descriptor = typed_tool_registry().get(name)
    context = local_owner_context(correlation_id="corr-" + uuid.uuid4().hex)
    if principal != "owner":
        from dataclasses import replace

        context = replace(context, principal_id=principal)
    return executor.execute(descriptor, ToolCall(name, arguments), context, ExecutionClass.HOST)


_MINIMAL = {
    "build_model": {}, "build_job": {"target": "game"}, "build_job_result": {"job_id": "build-job-" + "a" * 32},
    "build_fix": {"target": "game"}, "build_fix_result": {"job_id": "build-fix-" + "a" * 32},
    "build_fix_restore": {"job_id": "build-fix-" + "a" * 32},
}


@pytest.mark.parametrize("name", BUILD_TYPED_TOOLS)
def test_uncomposed_services_answer_build_tools_unavailable(name):
    result = _execute(BuildToolExecutor(None, PackagedToolExecutor()), name, _MINIMAL[name])
    assert not result.success and result.error_code == BUILD_TOOLS_UNAVAILABLE
    assert json.loads(result.output) == {"ok": False, "error_code": BUILD_TOOLS_UNAVAILABLE,
                                         "message": "build tools are not composed in this runtime"}


@pytest.mark.parametrize("name", ["build_fix", "build_fix_result", "build_fix_restore"])
def test_fix_tools_are_unavailable_without_the_fix_loop(name):
    executor = BuildToolExecutor(fake_services(fix=False), PackagedToolExecutor())
    result = _execute(executor, name, _MINIMAL[name])
    assert result.error_code == BUILD_TOOLS_UNAVAILABLE
    assert _execute(executor, "build_model", {}).success


@pytest.mark.parametrize("missing, tool, arguments", [
    ("sonder_runtime.application.build.ports", "build_model", {}),
    ("sonder_runtime.application.build.ports", "build_job", {"target": "game"}),
    ("sonder_runtime.application.build.fix_ports", "build_fix", {"target": "game"}),
])
def test_missing_build_packages_report_unavailable_not_a_crash(monkeypatch, missing, tool, arguments):
    # A None entry makes the import fail, as in a runtime shipped without the package.
    monkeypatch.setitem(sys.modules, missing, None)
    result = _execute(BuildToolExecutor(fake_services(), PackagedToolExecutor()), tool, arguments)
    assert not result.success and result.error_code == BUILD_TOOLS_UNAVAILABLE


@pytest.mark.parametrize("code", sorted(KNOWN_ERROR_CODES - {BUILD_TOOLS_UNAVAILABLE}))
def test_every_build_error_code_maps_through(code):
    services = fake_services()

    def refuse(*args, **kwargs):
        raise coded(InvalidInput, code, "refused for the test")

    services.jobs.run = refuse
    result = _execute(BuildToolExecutor(services, PackagedToolExecutor()), "build_job", {"target": "game"})
    assert not result.success and result.error_code == code
    assert json.loads(result.output)["error_code"] == code


@pytest.mark.parametrize("exc, code", [
    (NotFound("gone"), "JOB_NOT_FOUND"), (CapacityExceeded("full"), "BUILD_BUSY"),
    (Conflict("held"), "BUILD_DIR_BUSY"), (Forbidden("no"), "BUILD_TREE_REJECTED"),
    (DependencyUnavailable("x"), "BUILD_MODEL_UNAVAILABLE"), (InvalidInput("bad"), "INVALID_INPUT"),
    (PermissionError("outside"), "PROJECT_OUTSIDE_ROOTS"),
    (PermissionError(13, "Permission denied", "/home/secret/build"), "PROJECT_OUTSIDE_ROOTS"),
    (FileNotFoundError("/home/secret/tree"), "BUILD_TREE_MISSING"),
    (OSError("/home/secret/path failed"), "HOST_IO_FAILURE"),
    (ValueError("bad value"), "INVALID_INPUT"),
])
def test_uncoded_failures_get_stable_codes_and_no_host_paths(exc, code):
    services = fake_services()

    def refuse(*args, **kwargs):
        raise exc

    services.jobs.run = refuse
    result = _execute(BuildToolExecutor(services, PackagedToolExecutor()), "build_job", {"target": "game"})
    assert result.error_code == code
    assert "/home/secret" not in result.output
    assert "/home/secret" not in (result.error or "")


def test_handlers_map_arguments_to_requests():
    services = fake_services()
    executor = BuildToolExecutor(services, PackagedToolExecutor())
    result = _execute(executor, "build_model", {"detail": "targets", "max_items": 900, "preset": "ninja-debug"})
    body = json.loads(result.output)
    assert result.success and body["ok"] and body["detail"] == "targets"
    request, principal, detail = services.model.views[-1]
    assert request.preset == "ninja-debug" and principal == "owner"
    result = _execute(executor, "build_job", {"target": "game", "config": "Debug", "jobs": 999,
                                              "action": "compile_one", "file": "src/a.cpp",
                                              "wait_seconds": 500})
    assert result.success, result.output
    request, wait, _ = services.jobs.runs[-1]
    assert (request.action, request.config, request.jobs, request.file, wait) == (
        "compile_one", "Debug", 256, "src/a.cpp", 120)
    for bad in ({"action": "rebuild"}, {"detail": "everything"}, {"jobs": "4"}):
        name = "build_model" if "detail" in bad else "build_job"
        refused = _execute(executor, name, bad)
        assert not refused.success and refused.error_code == "INVALID_INPUT", bad


@pytest.mark.parametrize("tool, arguments", [
    ("build_job", {"target": "-DCMAKE_CXX_COMPILER=/tmp/evil"}),
    ("build_job", {"target": "--target"}),
    ("build_job", {"target": "@/tmp/response.rsp"}),
    ("build_job", {"target": "game;rm -rf ~"}),
    ("build_job", {"target": "game\ninstall"}),
    ("build_job", {"target": "$(touch x)"}),
    ("build_job", {"target": "game install"}),
    ("build_job", {"target": "game:Rebuild"}),
    ("build_job", {"target": "a:b:Build"}),
    ("build_job", {"target": "\\\\server\\share"}),
    ("build_job", {"config": "Debug|x64"}),
    ("build_job", {"config": "/p:Configuration=Release"}),
    ("build_job", {"platform": " x64"}),
    ("build_job", {"platform": "x64\t--"}),
    ("build_job", {"preset": "--trace-expand"}),
    ("build_job", {"build_preset": "-P"}),
    ("build_job", {"profile": "p=1"}),
    ("build_job", {"generator": "Ninja\n-DX=1"}),
    ("build_job", {"file": "@src/a.rsp"}),
    ("build_job", {"file": "-include/etc/passwd"}),
    ("build_job", {"build_dir": "\\\\server\\share\\build"}),
    ("build_job", {"build_dir": "//server/share/build"}),
    ("build_job", {"project": "src\n..", "target": "game"}),
    ("build_model", {"preset": "--debug-trycompile"}),
    ("build_model", {"target": "-t"}),
    ("build_fix", {"target": "--target"}),
    ("build_fix", {"target": "game", "editable_globs": ["../outside/*.cpp"]}),
    ("build_fix", {"target": "game", "editable_globs": ["/etc/*"]}),
    ("build_fix", {"target": "game", "editable_globs": ["C:\\Windows\\*.dll"]}),
    ("build_fix", {"target": "game", "focus_file": "\\\\host\\share\\a.cpp"}),
])
def test_option_and_command_shaped_names_are_refused_before_planning(tool, arguments):
    services = fake_services()
    executor = BuildToolExecutor(services, PackagedToolExecutor())
    refused = _execute(executor, tool, arguments)
    assert not refused.success and refused.error_code == "INVALID_INPUT", refused.output
    assert not services.jobs.runs and not services.jobs.planned and not services.model.views


@pytest.mark.parametrize("arguments", [
    {"target": "game", "config": "RelWithDebInfo", "platform": "Gaming.Xbox.Scarlett.x64"},
    {"target": "Engine_Core:Build", "config": "Debug", "platform": "Any CPU"},
    {"target": "my-lib.test", "preset": "ninja-debug", "build_preset": "ninja_debug2"},
    {"target": "Tools\\ShaderGen", "file": "src/a b.cpp"},
])
def test_legitimate_model_names_still_pass(arguments):
    services = fake_services()
    executor = BuildToolExecutor(services, PackagedToolExecutor())
    result = _execute(executor, "build_job", arguments)
    # The surface admits them; membership in the model is the planner's call.
    assert result.success or result.error_code != "INVALID_INPUT", result.output
    assert services.jobs.runs or result.error_code == "UNKNOWN_TARGET"


def test_results_and_cancel_are_owner_scoped():
    services = fake_services()
    executor = BuildToolExecutor(services, PackagedToolExecutor())
    job_id = json.loads(_execute(executor, "build_job", {"target": "game"}).output)["job_id"]
    report = json.loads(_execute(executor, "build_job_result", {"job_id": job_id}).output)
    assert report["status"] == "failed" and report["first_errors"]
    foreign = _execute(executor, "build_job_result", {"job_id": job_id}, principal="account:b")
    assert foreign.error_code == "JOB_NOT_FOUND"
    assert _execute(executor, "build_job_result", {"job_id": job_id, "cancel": True},
                    principal="account:b").error_code == "JOB_NOT_FOUND"
    cancelled = json.loads(_execute(executor, "build_job_result", {"job_id": job_id, "cancel": True}).output)
    assert cancelled["status"] == "cancelled" and services.jobs.cancelled == [job_id]


def test_fix_start_restore_and_conflict():
    services = fake_services()
    executor = BuildToolExecutor(services, PackagedToolExecutor(), grants=BuildFixGrantRegistry())
    started = json.loads(_execute(executor, "build_fix", {"target": "game", "attempts": 20}).output)
    assert started["status"] == "running" and started["grant"] == "none"
    request, principal, token = services.fix.started[-1]
    assert request.attempts == 8 and principal == "owner" and token == ""
    job_id = started["job_id"]
    restored = json.loads(_execute(executor, "build_fix_restore",
                                   {"job_id": job_id, "files": ["src/a.cpp"]}).output)
    assert restored["restored"] == ["src/a.cpp"]
    conflict = _execute(executor, "build_fix_restore", {"job_id": job_id, "files": ["changed.cpp"]})
    assert conflict.error_code == "RESTORE_CONFLICT"
    too_many = _execute(executor, "build_fix_restore", {"job_id": job_id, "files": ["a.cpp"] * 7})
    assert too_many.error_code == "INVALID_INPUT"
    assert _execute(executor, "build_fix", {}).error_code == "INVALID_INPUT"


def test_other_tools_fall_through_to_the_developer_executor(tmp_path):
    from tests.test_tools_test_runs_fakes import services as developer_services
    from sonder_runtime.adapters.developer_tools_executor import DeveloperToolExecutor

    developer = developer_services()
    chained = BuildToolExecutor(None, DeveloperToolExecutor(developer, PackagedToolExecutor(),
                                                            inventory_wire=lambda view: view))
    for name, arguments in (("tool_inventory", {}), ("output_digest", {"path": "build.log"}),
                            ("test_run", {"runner": "pytest"})):
        result = _execute(chained, name, arguments)
        assert result.success, (name, result.output)
    assert developer.test_runs.runs


def test_payloads_fit_the_wire_bound():
    payload = {"ok": True, "first_errors": ["x" * 400] * 400, "attributions": [{"a": "y" * 300}] * 400}
    fitted = fit_payload(payload)
    assert len(json.dumps(fitted, separators=(",", ":")).encode()) <= MAX_WIRE_BYTES
    assert fitted["truncated"] is True and fitted["first_errors"]


def test_the_executor_module_has_no_host_process_or_network_imports():
    source = open(os.path.join(os.path.dirname(__file__), "..", "sonder_runtime", "adapters",
                               "build", "executor.py"), encoding="utf-8").read()
    for forbidden in ("import subprocess", "import socket", "urllib", "os.environ"):
        assert forbidden not in source
