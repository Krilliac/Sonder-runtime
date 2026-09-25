"""The build-fix grant (F1): one approval of build_fix covers the fix's own
in-scope writes, unattended, for as long as the fix job lives -- and nothing else.

The writes go through the real typed gateway and the real guarded write
primitives (``text_patch``/``write_file``), so the receipts, the audit and the
files on disk are the production ones. ``manual`` mode is the default and the
worker source is unattended: without the grant every write here is refused.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.bootstrap.build_tools import (
    GRANT_POLICY_PREFIX,
    BuildFixGrantRegistry,
    build_permission_resolvers,
)
from sonder_runtime.bootstrap.developer_tools import ToolResolver
from sonder_runtime.bootstrap.lane_tests import CatalogPermissionEvaluator
from sonder_runtime.domain.common.errors import Forbidden
from tests.test_build_executor import (
    FakeFix,
    compose_facade,
    fake_services,
    gateway_call,
    output,
    port_doubles,  # noqa: F401 - fixture
)

pytestmark = pytest.mark.unit

SOURCE = "int area(int w, int h) {\n  return w * lenght;\n}\n"
PATCH = ("--- a/src/core/math.cpp\n+++ b/src/core/math.cpp\n@@ -1,3 +1,3 @@\n"
         " int area(int w, int h) {\n-  return w * lenght;\n+  return w * h;\n }\n")


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "sparklite"
    for rel, text in (("src/core/math.cpp", SOURCE), ("src/game/main.cpp", "int main() {}\n"),
                      ("tools/shadergen.cpp", "int main() {}\n"), ("CMakeLists.txt", "project(x)\n"),
                      (".env", "TOKEN=x\n"), ("build/gen/out.cpp", "int g;\n")):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for index in range(8):
        (root / "src" / ("f%d.cpp" % index)).write_text("int f%d;\n" % index, encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    pm.reset_unattended_for_tests()
    yield root
    pm.reset_unattended_for_tests()


@pytest.fixture
def stack(tmp_path, project, port_doubles):
    clock = Clock()
    services = fake_services(project, project / "build", clock=clock)
    grants = BuildFixGrantRegistry(clock=clock, current_mode=lambda: pm.current_mode())
    tools, audit, _ = compose_facade(tmp_path, services, grants=grants)
    # The console operator approves build_fix once (the REPL prompt; gate=surface).
    started = gateway_call(tools, "build_fix", {"target": "game"}, source="repl", gate="surface")
    assert started.success, started.output
    job_id = output(started)["job_id"]
    token = services.fix.started[-1][2]
    assert token.startswith("build_fix_grant:"), "the approval became the job's grant"
    return SimpleNamespace(tools=tools, audit=audit, grants=grants, services=services,
                           job_id=job_id, token=token, clock=clock, root=project)


def _write(stack, tool, arguments, *, token=None, principal="owner"):
    return gateway_call(stack.tools, tool, arguments, source="worker", principal=principal,
                        token=stack.token if token is None else token)


def test_an_in_scope_patch_passes_unattended_and_is_receipted_as_the_grant(stack):
    receipt = _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH, "apply": True})
    assert receipt.success, receipt.output
    assert GRANT_POLICY_PREFIX in receipt.policy_match
    assert (stack.root / "src/core/math.cpp").read_text() == SOURCE.replace("lenght", "h")
    audited = stack.audit.read()[-1]
    assert audited["terminal"] == "completed"
    assert GRANT_POLICY_PREFIX in audited["policy_match"]
    write = _write(stack, "write_file", {"path": str(stack.root / "src/game/main.cpp"),
                                         "content": "int main() { return 0; }\n", "mode": "overwrite"})
    assert write.success, write.output
    read = _write(stack, "read_file", {"path": str(stack.root / "src/game/main.cpp")})
    assert read.success


def test_without_the_token_the_same_write_is_refused_unattended(stack):
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH}, token="")
    assert (stack.root / "src/core/math.cpp").read_text() == SOURCE


@pytest.mark.parametrize("rel, why", [
    ("tools/shadergen.cpp", "build-time tool source"),
    ("CMakeLists.txt", "build script"),
    (".env", "denied name"),
    ("build/gen/out.cpp", "build directory"),
])
def test_out_of_scope_files_fall_through_and_are_refused(stack, rel, why):
    before = (stack.root / rel).read_bytes()
    with pytest.raises(Forbidden):
        _write(stack, "write_file", {"path": str(stack.root / rel), "content": "x\n",
                                     "mode": "overwrite"})
    assert (stack.root / rel).read_bytes() == before, why


def test_paths_outside_relative_or_linked_are_not_covered(stack, tmp_path):
    outside = tmp_path / "outside.cpp"
    outside.write_text("int o;\n")
    link = stack.root / "src" / "link.cpp"
    os.symlink(outside, link)
    for arguments in ({"path": str(outside), "content": "x", "mode": "overwrite"},
                      {"path": "src/core/math.cpp", "content": "x", "mode": "overwrite"},
                      {"path": str(link), "content": "x", "mode": "overwrite"},
                      {"path": str(stack.root / "src/core/math.cpp"), "content": "x", "mode": "append"},
                      {"path": str(stack.root / "src/new.cpp"), "content": "x", "mode": "overwrite"}):
        with pytest.raises(Forbidden):
            _write(stack, "write_file", arguments)
    assert outside.read_text() == "int o;\n"
    escaping = PATCH.replace("src/core/math.cpp", "../outside.cpp")
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": escaping})
    with pytest.raises(Forbidden):  # guard knobs are never covered
        _write(stack, "write_file", {"path": str(stack.root / "src/f0.cpp"), "content": "x",
                                     "mode": "overwrite", "extra_roots": str(tmp_path)})


def test_the_grant_ends_with_its_job(stack):
    stack.services.fix.finish(stack.job_id)
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH})
    assert len(stack.grants) == 0


def test_the_grant_expires(stack):
    stack.clock.now += 4 * 3600
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH})


def test_a_foreign_principal_cannot_spend_the_grant(stack):
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH}, principal="account:b")
    assert (stack.root / "src/core/math.cpp").read_text() == SOURCE


def test_plan_mode_is_never_lifted_by_the_grant(stack, monkeypatch):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    with pytest.raises(Forbidden):
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH})


def test_an_explicit_deny_rule_outranks_the_grant(stack, monkeypatch):
    # The grant answers the mode's unattended ask; it never softens a written deny.
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: {"action": "deny", "pattern": "*"})
    with pytest.raises(Forbidden) as caught:
        _write(stack, "text_patch", {"root": str(stack.root), "patch": PATCH})
    assert caught.value.decision["source"] == "rule"
    assert (stack.root / "src/core/math.cpp").read_text() == SOURCE


def test_a_lost_effect_fence_outranks_the_grant(stack):
    from sonder_runtime.adapters.execution import effect_fence

    lost = effect_fence.Fence("test lease", lambda: "lease lost")
    with effect_fence.held(lost), pytest.raises(Forbidden) as caught:
        _write(stack, "write_file", {"path": str(stack.root / "src/f0.cpp"), "content": "int z;\n",
                                     "mode": "overwrite"})
    assert caught.value.decision["source"] == "fence"
    assert (stack.root / "src/f0.cpp").read_text() == "int f0;\n"


def test_file_and_line_budgets_hold(stack):
    for index in range(6):
        path = stack.root / "src" / ("f%d.cpp" % index)
        assert _write(stack, "write_file", {"path": str(path), "content": "int g%d;\n" % index,
                                            "mode": "overwrite"}).success
    with pytest.raises(Forbidden):  # a seventh file
        _write(stack, "write_file", {"path": str(stack.root / "src/f6.cpp"), "content": "int z;\n",
                                     "mode": "overwrite"})
    # an already-touched file still fits, but not 400+ changed lines
    big = "".join("int v%d;\n" % n for n in range(500))
    with pytest.raises(Forbidden):
        _write(stack, "write_file", {"path": str(stack.root / "src/f0.cpp"), "content": big,
                                     "mode": "overwrite"})


def test_the_cumulative_line_budget_holds_across_writes(stack):
    # Each write stays under the per-write cap, but the job's writes together
    # ('-' and '+' each count) may not exceed 4 x max_changed_lines: a
    # candidate and its revert for every line the loop may change.
    path = stack.root / "src" / "f0.cpp"
    first = "".join("int a%d;\n" % n for n in range(350))
    second = "".join("int b%d;\n" % n for n in range(350))
    assert _write(stack, "write_file", {"path": str(path), "content": first,
                                        "mode": "overwrite"}).success
    assert _write(stack, "write_file", {"path": str(path), "content": second,
                                        "mode": "overwrite"}).success
    with pytest.raises(Forbidden):
        _write(stack, "write_file", {"path": str(path), "content": first, "mode": "overwrite"})
    assert path.read_text() == second


def test_a_build_dir_at_the_project_root_leaves_nothing_writable(tmp_path):
    from sonder_runtime.application.build.grants import BuildFixGrantSpec
    from sonder_runtime.bootstrap.build_tools import _OutOfScope

    source = tmp_path / "a.cpp"
    source.write_text("int a;\n")
    spec = BuildFixGrantSpec(project_root=str(tmp_path), build_dir=str(tmp_path), target="game")
    with pytest.raises(_OutOfScope):
        BuildFixGrantRegistry._in_root(spec, str(source))


def test_the_grant_carries_network_only_when_the_fix_was_approved_with_it(stack, monkeypatch):
    child = SimpleNamespace(template_id="cmake.build", network="enforced_off", target="game",
                            config="", platform="", world="host", build_dir=str(stack.root / "build"))
    assert stack.grants.covers_child_build(stack.token, "owner", child)
    assert not stack.grants.covers_child_build(stack.token, "owner",
                                               SimpleNamespace(**{**vars(child), "network": "allowed"}))
    assert not stack.grants.covers_child_build(stack.token, "owner",
                                               SimpleNamespace(**{**vars(child), "template_id": "make.build"}))
    assert not stack.grants.covers_child_build(stack.token, "owner",
                                               SimpleNamespace(**{**vars(child), "target": "core"}))
    assert not stack.grants.covers_child_build(stack.token, "account:b", child)
    # A fix approved with allow_network (auto mode, no deny rule) carries it.
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    started = gateway_call(stack.tools, "build_fix", {"target": "game", "allow_network": True})
    token = stack.services.fix.started[-1][2]
    assert started.success and token
    assert stack.grants.covers_child_build(token, "owner",
                                           SimpleNamespace(**{**vars(child), "network": "allowed"}))


def test_an_unclaimed_grant_dies_and_claims_are_single_use(port_doubles, tmp_path):
    clock = Clock()
    grants = BuildFixGrantRegistry(clock=clock)
    fix = FakeFix(tmp_path, tmp_path / "build", clock=clock, grants=grants)
    from tests.test_build_executor import FixRequestDouble

    request = FixRequestDouble(target="game")
    plan = fix.plan(request, None)
    grants.mint(principal_id="owner", request_id="r1", plan=plan)
    assert grants.claim("r1", "account:b") is None
    assert grants.claim("r1", "owner") is plan
    assert grants.claim("r1", "owner") is None, "one approval, one claim"
    # The claim approved the plan once: one grant is issued, the next start has none.
    ctx = SimpleNamespace(principal_id="owner")
    first = fix.start(request, ctx, plan=plan)
    second = fix.start(request, ctx, plan=plan)
    assert fix.started[-2][2] and not fix.started[-1][2]
    assert grants.granted_job(first) and not grants.granted_job(second)
    grants.mint(principal_id="owner", request_id="r2", plan=plan)
    clock.now += 301
    assert grants.claim("r2", "owner") is None


def test_a_failed_start_leaves_no_approval_behind(port_doubles, tmp_path):
    from tests.test_build_executor import FixRequestDouble

    grants = BuildFixGrantRegistry()
    grants.mint(principal_id="owner", request_id="r1",
                plan=FakeFix(tmp_path, tmp_path / "build").plan(FixRequestDouble(target="game"), None))
    plan = grants.claim("r1", "owner")
    assert grants.approved(plan.plan_digest, "owner")
    grants.withdraw(plan.plan_digest, "owner")
    assert not grants.approved(plan.plan_digest, "owner") and len(grants) == 0


def test_a_mismatched_plan_is_refused_by_the_real_service(port_doubles):
    from sonder_runtime.application.build.fix_ports import BuildFixRequest
    from sonder_runtime.application.build.fix_service import BuildFixService

    service = BuildFixService(None, None, None, None, None, None, None, None, None, clock=time.time)
    ctx = SimpleNamespace(expired=False, cancellation=SimpleNamespace(cancelled=False))
    with pytest.raises(Exception) as caught:
        service.start(BuildFixRequest(target="game"), ctx,
                      plan=SimpleNamespace(request=BuildFixRequest(target="core")))
    assert "does not match" in str(caught.value)


def test_lane_tests_evaluator_keeps_test_run_and_gains_the_build_resolvers(port_doubles, tmp_path):
    from tests.test_tools_test_runs_fakes import services as developer_services

    catalog = SimpleNamespace(targets={}, require_current=lambda: None, digest="d")
    grants = BuildFixGrantRegistry()
    evaluator = CatalogPermissionEvaluator(
        catalog, developer_services(),
        resolvers=build_permission_resolvers(fake_services(tmp_path, tmp_path / "b"), grants=grants),
        grant_authorities=(grants,),
    )
    assert {"test_run", "build_job", "build_fix"} <= set(evaluator.resolvers)
    assert all(isinstance(item, ToolResolver) for item in evaluator.resolvers.values())
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGatewayRequest, ToolPermission, ToolScope,
    )

    request = ToolGatewayRequest("r", "test_run", {"runner": "pytest"},
                                 ToolScope(principal_id="owner", source="mcp"), ToolPermission())
    resolved = evaluator.resolvers["test_run"].resolve(request)
    assert resolved.arguments["resolved_command"]["runner"] == "pytest"
    # the unchanged default: a lane evaluator without build resolvers still builds
    bare = CatalogPermissionEvaluator(catalog, developer_services())
    assert set(bare.resolvers) == {"test_run"}
