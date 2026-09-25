"""Live smoke: the composed build tools on a real CMake + Ninja project.

Under ``auto`` mode, through the real typed gateway and the production
composition (``compose_build_tools``), with the durable SQLite job registry
and the process-job provider:

1. ``build_model`` before configure answers ``BUILD_TREE_MISSING``;
2. ``build_job configure`` then ``build_model`` lists the targets;
3. ``build_job build`` fails on the seeded error, attributed to its file;
4. ``build_fix`` (when the fix loop is composed) repairs it with a scripted
   generator that edits only in-scope files; otherwise the repair is an
   in-scope ``write_file`` through the same gateway;
5. ``build_job build`` passes.

Runs for g++ and clang++. Skips when the build packages (lanes A/B1) or the
host tools are missing; when the fix-loop package (lane B2) is importable but
does not compose, the test fails rather than skipping, so a composition
mismatch surfaces at merge.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

import permission_modes as pm

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.name == "nt", reason="POSIX process groups on this host")]


def _importable(*names):
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError:
            return False
    return True


BUILD_STACK = _importable("sonder_runtime.domain.build.model", "sonder_runtime.domain.build.templates",
                          "sonder_runtime.application.build.run_service",
                          "sonder_runtime.adapters.build.planner",
                          "sonder_runtime.adapters.build.launcher")
FIX_STACK = _importable("sonder_runtime.application.build.fix_service",
                        "sonder_runtime.adapters.build.source_editor")
HOST_TOOLS = all(shutil.which(name) for name in ("cmake", "ninja"))

MATH_BAD = "#include \"math.h\"\nint area(int w, int h) {\n  return w * lenght;\n}\n"
MATH_GOOD = "#include \"math.h\"\nint area(int w, int h) {\n  return w * h;\n}\n"


@dataclass(frozen=True)
class HostRecord:
    name: str
    path: str
    version: str = ""
    details: tuple = ()


class HostLookup:
    """``HostToolLookup`` over PATH (the inventory's contract, without discovery)."""

    def lookup(self, name):
        path = shutil.which(name)
        return None if path is None else HostRecord(name, os.path.abspath(path))

    def capability_summary(self, *, max_chars=480):
        return ""


def _project(root: Path, compiler: str) -> None:
    (root / "src" / "core").mkdir(parents=True)
    (root / "src" / "game").mkdir(parents=True)
    (root / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "set(CMAKE_CXX_COMPILER \"%s\")\n"
        "project(sparklite CXX)\n"
        "add_library(core STATIC src/core/math.cpp)\n"
        "target_include_directories(core PUBLIC src/core)\n"
        "add_executable(game src/game/main.cpp)\n"
        "target_link_libraries(game PRIVATE core)\n"
        "add_custom_target(deploy COMMAND ${CMAKE_COMMAND} -E echo deploy)\n" % compiler)
    (root / "src/core/math.h").write_text("#pragma once\nint area(int w, int h);\n")
    (root / "src/core/math.cpp").write_text(MATH_BAD)
    (root / "src/game/main.cpp").write_text("#include \"math.h\"\nint main() { return area(2, 3) - 6; }\n")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
    from sonder_runtime.adapters.persistence.tool_audit import DurableToolAuditRepository
    from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
    from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor
    from sonder_runtime.application.tools.facade import ReceiptStore, ToolApplicationFacade
    from sonder_runtime.bootstrap.build_tools import (
        BuildFixGrantRegistry,
        build_permission_resolvers,
        build_tool_executor,
        compose_build_tools,
    )
    from sonder_runtime.bootstrap.developer_tools import DeveloperToolPermissionEvaluator
    from sonder_runtime.bootstrap.diagnostics import compose_output_digest_service
    from sonder_runtime.bootstrap.typed_tools import (
        POLICY_NAMES,
        typed_tool_policy,
        typed_tool_registry,
    )
    from sonder_runtime.platform import paths as runtime_paths

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    previous = runtime_paths._configured_home()
    runtime_paths.configure_home(tmp_path / "home")
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    provider = SubprocessJobProvider(registry, process_cleanup=ProcessTreeSupervisor())
    grants = BuildFixGrantRegistry(current_mode=lambda: pm.current_mode())
    holder: dict = {}
    services = compose_build_tools(
        config=None, inventory=HostLookup(),
        digest=compose_output_digest_service(lambda: registry),
        process_job_provider=lambda: provider, job_registry=lambda: registry, redactor=None,
        grants=grants, tools_getter=lambda: holder.get("tools"),
        candidate_generator=ScriptedGenerator() if FIX_STACK else None,
    )
    assert services is not None, "the build packages are present but did not compose"
    tools = ToolApplicationFacade.compose(
        typed_tool_registry(),
        build_tool_executor(services, PackagedToolExecutor(), grants=grants),
        policy=typed_tool_policy(), receipts=ReceiptStore(),
        audit=DurableToolAuditRepository(tmp_path / "audit.jsonl"),
        permissions=(DeveloperToolPermissionEvaluator(
            None, policy_names=POLICY_NAMES,
            resolvers=build_permission_resolvers(services, grants=grants),
            grant_authorities=(grants,)),),
    )
    holder["tools"] = tools
    try:
        yield tools, services, workspace
    finally:
        if previous is None:
            runtime_paths.reset_home()
        else:
            runtime_paths.configure_home(previous)


class ScriptedGenerator:
    """A deterministic candidate generator: replace the seeded typo, in scope only."""

    def __init__(self):
        self.calls = 0

    def propose(self, evidence, ctx, *, route_hint=""):
        from sonder_runtime.domain.build.repair import CandidatePatch, PatchHunk

        self.calls += 1
        return CandidatePatch(
            hunks=(PatchHunk(file_rel="src/core/math.cpp", anchor="return w * lenght;",
                             replacement="return w * h;"),),
            rationale="the parameter is named h", model_id="scripted")


def _call(tools, name, arguments):
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest,
        ToolPermission,
        ToolScope,
    )

    descriptor = tools.graph.registry.get(name)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    receipt = tools.execute(ToolGatewayRequest(
        "live-" + uuid.uuid4().hex, name, arguments,
        ToolScope(principal_id="owner", source="mcp", allowed_effects=effects),
        ToolPermission(effects)))
    return receipt, json.loads(receipt.output)


def _wait_job(tools, body, tool="build_job_result", limit=240):
    deadline = time.monotonic() + limit
    while body.get("status") in ("running", "pending") and time.monotonic() < deadline:
        _, body = _call(tools, tool, {"job_id": body["job_id"], "wait_seconds": 30})
    return body


@pytest.mark.skipif(not (BUILD_STACK and HOST_TOOLS), reason="needs lanes A/B1 and cmake+ninja")
@pytest.mark.parametrize("compiler", ["g++", "clang++"])
def test_model_build_fix_build(runtime, compiler):
    if not shutil.which(compiler):
        pytest.skip("%s is not installed" % compiler)
    tools, services, workspace = runtime
    project = workspace / ("sparklite-" + compiler.replace("+", "p"))
    _project(project, shutil.which(compiler))
    common = {"project": str(project), "build_dir": str(project / "build")}

    receipt, body = _call(tools, "build_model", common)
    assert body.get("error_code") == "BUILD_TREE_MISSING", body

    _, body = _call(tools, "build_job", {**common, "action": "configure", "generator": "Ninja",
                                         "wait_seconds": 120})
    body = _wait_job(tools, body)
    assert body["status"] == "succeeded", body

    _, model = _call(tools, "build_model", {**common, "detail": "targets"})
    names = json.dumps(model)
    assert "game" in names and "core" in names, model
    assert str(project) not in names, "labels only on the wire"

    from sonder_runtime.domain.common.errors import Forbidden

    with pytest.raises(Forbidden) as refused:  # refused at planning, before any prompt or launch
        _call(tools, "build_job", {**common, "target": "deploy"})
    assert refused.value.decision["error_code"] == "UTILITY_TARGET_REFUSED"

    _, body = _call(tools, "build_job", {**common, "target": "game", "wait_seconds": 120})
    report = _wait_job(tools, body)
    assert report["status"] == "failed", report
    assert "math.cpp" in json.dumps(report.get("first_errors") or report)

    if FIX_STACK:
        assert services.fix is not None, "the fix-loop package is present but did not compose"
        _, started = _call(tools, "build_fix", {**common, "target": "game", "attempts": 3})
        assert started.get("ok"), started
        fixed = _wait_job(tools, started, tool="build_fix_result", limit=600)
        assert fixed["status"] == "fixed", fixed
        changed = {item.get("rel") for item in fixed.get("files", [])}
        assert changed == {"src/core/math.cpp"}
    else:
        receipt, body = _call(tools, "write_file", {"path": str(project / "src/core/math.cpp"),
                                                    "content": MATH_GOOD, "mode": "overwrite"})
        assert receipt.success, body
    assert (project / "src/core/math.cpp").read_text() == MATH_GOOD
    assert (project / "CMakeLists.txt").read_text().startswith("cmake_minimum_required")

    _, body = _call(tools, "build_job", {**common, "target": "game", "wait_seconds": 120})
    report = _wait_job(tools, body)
    assert report["status"] == "succeeded", report
