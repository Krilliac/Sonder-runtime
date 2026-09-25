"""End-to-end build fix on a real CMake + Ninja tree with g++ and clang++.

Production pieces all the way down: the build model and job services with
the guarded tree reader, planner, scrubbed environment, durable launcher and
collector (lane B1); the fix loop, strategy bridge, pre-image store and the
gateway source editor (this lane); the real typed tool gateway with the
packaged file primitives and the SQLite effect journal. One stand-in:

* the candidate generator is scripted and deterministic (a hostile patch,
  then a regression, then the fix);
* nothing else: the permission evaluator is lane C's real
  ``DeveloperToolPermissionEvaluator`` with the real ``BuildFixGrantRegistry``
  as its grant authority, under ``manual`` mode with the fix running as an
  unattended worker, so a write is admitted only when the fix's grant covers
  it (the registry is also the grant book the service issues from).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

import pytest

import permission_modes as pm

pytest.importorskip("sonder_runtime.domain.build.repair", reason="needs lane A-domain-build")
pytest.importorskip("sonder_runtime.adapters.build.launcher", reason="needs lane B1-run-and-model")

from sonder_runtime.adapters.build.collector import BuildOutputCollector  # noqa: E402
from sonder_runtime.adapters.build.environment import ScrubbedEnvironmentProvider  # noqa: E402
from sonder_runtime.adapters.build.launcher import ProcessBuildLauncher  # noqa: E402
from sonder_runtime.adapters.build.network import NetworkIsolation  # noqa: E402
from sonder_runtime.adapters.build.planner import ProjectBuildPlanner  # noqa: E402
from sonder_runtime.adapters.build.preimages import FilePreimageStore  # noqa: E402
from sonder_runtime.adapters.build.source_editor import GatewaySourceEditor  # noqa: E402
from sonder_runtime.adapters.build.tree_reader import GuardedBuildTreeReader  # noqa: E402
from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider  # noqa: E402
from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal  # noqa: E402
from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry  # noqa: E402
from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor  # noqa: E402
from sonder_runtime.adapters.typed_tool_executor import PackagedToolExecutor  # noqa: E402
from sonder_runtime.application.build.fix_ports import BuildFixRequest, EditContext, EditRefused  # noqa: E402
from sonder_runtime.application.build.fix_service import BuildFixService  # noqa: E402
from sonder_runtime.application.build.model_service import BuildModelService, LruBuildModelCache  # noqa: E402
from sonder_runtime.application.build.ports import BuildJobRequest  # noqa: E402
from sonder_runtime.application.build.run_service import (  # noqa: E402
    BuildJobService,
    InMemoryBuildDirLeases,
    build_job_liveness,
)
from sonder_runtime.application.build.strategy_bridge import StrategyFixAdapter  # noqa: E402
from sonder_runtime.application.cancellation_tree import CancellationTree  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.execution.effect_journal import EffectState  # noqa: E402
from sonder_runtime.application.ports.jobs import JobStatus  # noqa: E402
from sonder_runtime.application.tools.facade import ToolApplicationFacade  # noqa: E402
from sonder_runtime.bootstrap.build_tools import BuildFixGrantRegistry  # noqa: E402
from sonder_runtime.bootstrap.developer_tools import DeveloperToolPermissionEvaluator  # noqa: E402
from sonder_runtime.bootstrap.typed_tools import (  # noqa: E402
    POLICY_NAMES,
    typed_tool_policy,
    typed_tool_registry,
)
from sonder_runtime.domain.build.repair import CandidatePatch, FixStopReason, PatchHunk  # noqa: E402
from sonder_runtime.domain.build.report import BuildJobReport  # noqa: E402
from sonder_runtime.domain.common.errors import Forbidden  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name == "nt", reason="POSIX process groups; Windows runs the MSVC fakes"),
]

FIXTURE = Path(__file__).parent / "fixtures" / "cpp_build" / "sparklite"
MATH = "src/core/math.cpp"
HAVE_TOOLS = all(shutil.which(tool) for tool in ("cmake", "ninja"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HostRecord:
    def __init__(self, name, path):
        self.name, self.path, self.version, self.details = name, path, "", ()


class HostLookup:
    def lookup(self, name):
        path = shutil.which(name)
        return None if path is None else HostRecord(name, os.path.abspath(path))


def host_executable_guard(path: str) -> str:
    if not Path(path).is_absolute() or not Path(path).resolve().is_file():
        raise PermissionError("host executable rejected")
    return path


class ScriptedGenerator:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def propose(self, evidence, context, *, route_hint=""):
        self.calls += 1
        assert "lenght" in evidence.source_window or self.calls > 1
        if not self.script:
            raise ValueError("nothing left")
        return self.script.pop(0)


def hunk(anchor, replacement):
    return CandidatePatch(hunks=(PatchHunk(MATH, anchor, replacement),), rationale="scripted",
                          model_id="scripted")


class Stack:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.allowed = tmp_path / "allowed"
        self.allowed.mkdir()
        monkeypatch.setenv("SONDER_FILE_ROOTS", str(self.allowed))
        state = tmp_path / "state"
        state.mkdir()
        self.registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
        provider = SubprocessJobProvider(self.registry, process_cleanup=ProcessTreeSupervisor())
        run_root = str(state / "build-runs")
        self.launcher = ProcessBuildLauncher(lambda: provider, lambda: self.registry,
                                             executable_guard=host_executable_guard, run_root=run_root)
        env = ScrubbedEnvironmentProvider(host="posix", project_local=lambda path: False)
        reader = GuardedBuildTreeReader()
        network = NetworkIsolation(mode="advisory", lookup=HostLookup(),
                                   executable_guard=host_executable_guard)
        self.planner = ProjectBuildPlanner(HostLookup(), reader, env, network, run_root=run_root,
                                           host="linux", executable_guard=host_executable_guard)
        self.models = BuildModelService(reader, self.planner, LruBuildModelCache(), clock=time.time)
        self.jobs = BuildJobService(self.planner, self.launcher, BuildOutputCollector(run_root),
                                    self.models,
                                    InMemoryBuildDirLeases(is_active=build_job_liveness(self.launcher)),
                                    clock=time.time)
        # manual mode, no rules, no one-shot approvals: an unattended write
        # passes only when the fix's grant covers it.
        monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
        monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
        monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
        pm.reset_unattended_for_tests()
        self.book = BuildFixGrantRegistry(clock=time.time, current_mode=lambda: pm.current_mode())
        self.evaluator = DeveloperToolPermissionEvaluator(
            None, policy_names=POLICY_NAMES, grant_authorities=(self.book,))
        self.tools = ToolApplicationFacade.compose(
            typed_tool_registry(), PackagedToolExecutor(), policy=typed_tool_policy(),
            permissions=(self.evaluator,),
        )
        self.editor = GatewaySourceEditor.over(self.tools)
        self.journal = SQLiteEffectJournal(tmp_path / "effects.db")
        self.preimages = FilePreimageStore(state / "build-fix")
        self.strategies = []

    def service(self, generator):
        def strategy():
            adapter = StrategyFixAdapter()
            self.strategies.append(adapter)
            return adapter

        return BuildFixService(
            self.jobs, self.models, self.editor, generator, strategy, None, self.preimages,
            self.registry, CancellationTree(), clock=time.time, grants=self.book,
            effect_journal_store=self.journal,
        )

    @staticmethod
    def context():
        # The fix runs unattended: nobody can answer a prompt for its writes.
        return local_owner_context(correlation_id=uuid.uuid4().hex, source="worker")

    def build(self, root, **kwargs):
        job = self.jobs.start(BuildJobRequest(project=str(root), **kwargs), self.context())
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = self.jobs.result(job, self.context(), wait_seconds=10)
            if isinstance(result, BuildJobReport):
                return result
        raise AssertionError("build job did not finish")


def project(stack, compiler):
    if not FIXTURE.is_dir():
        pytest.skip("the sparklite fixture (lane A) is missing")
    path = shutil.which(compiler)
    if path is None:
        pytest.skip("%s is not installed" % compiler)
    root = stack.allowed / ("sparklite-" + compiler.replace("+", "p"))
    shutil.copytree(FIXTURE, root)
    cmake = root / "CMakeLists.txt"
    cmake.write_text("set(CMAKE_CXX_COMPILER %s)\n" % path + cmake.read_text())
    report = stack.build(root, build_dir="build/ninja", action="configure", generator="Ninja",
                         config="Debug")
    assert report.status == "succeeded", (report.notes, report.first_errors)
    return root


@pytest.mark.skipif(not HAVE_TOOLS, reason="cmake and ninja are required")
@pytest.mark.parametrize("compiler", ["g++", "clang++"])
def test_the_fix_loop_repairs_the_seeded_error_end_to_end(tmp_path, monkeypatch, compiler):
    stack = Stack(tmp_path, monkeypatch)
    root = project(stack, compiler)
    original = (root / MATH).read_text()
    assert "lenght(v)" in original

    generator = ScriptedGenerator([
        # 1. A hostile candidate: refused by the scan, never written.
        hunk("float l = lenght(v);", '#include "/etc/shadow"\n    float l = length(v);'),
        # 2. A regression: more errors than before; reverted to the best.
        hunk("float l = lenght(v);", "float l = lenght(v) + nope_one + nope_two;"),
        # 3. The fix.
        hunk("float l = lenght(v);", "float l = length(v);"),
    ])
    service = stack.service(generator)
    request = BuildFixRequest(project=str(root), build_dir="build/ninja", target="core",
                              attempts=4, verify_dependents=True)
    context = stack.context()
    plan = service.plan(request, context)
    assert "build" in plan.scope.excluded_dirs[0]
    assert "tools/shadergen.cpp" in plan.scope.excluded_rel
    # Lane C's evaluator records the approval of this exact plan on ALLOW; the
    # executor claims it for its request and starts exactly that plan.
    stack.book.mint(principal_id=context.principal_id, request_id="approved-call", plan=plan)
    claimed = stack.book.claim("approved-call", context.principal_id)
    assert claimed is plan
    job = service.start(request, context, plan=claimed)
    report = service.result(job, context, wait_seconds=120)
    deadline = time.monotonic() + 600
    while not hasattr(report, "stop_reason") and time.monotonic() < deadline:
        report = service.result(job, context, wait_seconds=120)

    # The final build succeeds and the best candidate is what is on disk.
    assert report.stop_reason is FixStopReason.FIXED, (report.notes, report.attempts)
    assert report.status == "fixed" and report.final_build.status == "succeeded"
    # The fixture seeds a second, unrelated error in game: "fixed" is the target only.
    assert report.verification_scope == "target"
    assert any("dependents (all) do not: first error at src/game/main.cpp:10" in note
               for note in report.notes), report.notes
    fixed = (root / MATH).read_text()
    assert fixed == original.replace("float l = lenght(v);", "float l = length(v);")
    assert "/etc/shadow\"\n" not in fixed.split("*/")[-1] and "nope_one" not in fixed
    outcomes = [item.outcome for item in report.attempts]
    assert outcomes == ["rejected", "regressed", "fixed"], report.attempts
    assert any("HOSTILE_DIRECTIVE" in reason for reason in report.attempts[0].reasons)
    assert report.best.fixed and report.initial.errors_total >= 1

    # Pre-images are stored privately and verify.
    assert stack.preimages.load(job, MATH) == (original, sha(original))
    entry = stack.preimages.list(job)[0]
    assert entry.last_written_sha256 == sha(fixed)
    assert report.preimage_label.endswith(job) and str(root) not in report.preimage_label

    # The real typed gateway recorded an effect-journal intent for every write.
    intents = [intent for item in report.attempts for intent in item.effect_intent_ids]
    assert len(intents) == 2  # the regression's write and the fix's write
    for intent in intents:
        assert stack.journal.get(intent).state is EffectState.COMPLETED
    # The revert of the regression went through the gateway too: three writes in all.
    page = stack.journal.effects_since(job, 0, limit=50)
    assert len(page.records) == 3 and not page.unresolved
    assert all(record.state is EffectState.COMPLETED for record in page.records)
    written = [receipt for receipt in stack.tools.receipts if receipt.tool_name == "text_patch"]
    assert written and all(receipt.success for receipt in written)
    source = "build_fix_grant:" + plan.plan_digest
    assert all(source in receipt.policy_match.split(";") for receipt in written)

    # The job and its children are durable records; the grant died with the job.
    assert stack.registry.poll(job).status is JobStatus.SUCCEEDED
    children = stack.registry.list(parent_job_id=job, limit=50)
    assert children and all(child.identity.kind == "tool.build_job" for child in children)
    assert stack.book.live() == 0
    records = stack.strategies[0].records
    assert records and records[0].failure == "HYPOTHESIS_REJECTED"

    # build_fix_restore: unattended and unapproved, its writes are refused ...
    with pytest.raises(EditRefused):
        service.restore(job, context)
    assert (root / MATH).read_text() == fixed
    # ... the evaluator's approval of exactly this restore covers them, once.
    stack.book.mint_restore(principal_id=context.principal_id, request_id="restore-call",
                            job_id=job, files=())
    assert stack.book.claim_restore("restore-call", context.principal_id, job, ())
    restored = service.restore(job, context)
    assert restored["restored"] == [MATH] and (root / MATH).read_text() == original
    assert stack.book.live() == 0


@pytest.mark.skipif(not HAVE_TOOLS, reason="cmake and ninja are required")
def test_grant_scope_holds_on_the_real_gateway(tmp_path, monkeypatch):
    stack = Stack(tmp_path, monkeypatch)
    root = project(stack, "g++")
    service = stack.service(ScriptedGenerator([]))
    request = BuildFixRequest(project=str(root), build_dir="build/ninja", target="core")
    context = stack.context()
    plan = service.plan(request, context)
    stack.book.approve(plan.plan_digest, context.principal_id)
    grant = stack.book.issue(plan.grant_spec, principal_id=context.principal_id,
                             job_id="build-fix-" + "9" * 32, plan_digest=plan.plan_digest)
    edit = EditContext(operation=context, project_root=str(root), job_id=grant.job_id,
                       grant_token=grant.token)
    text, digest = stack.editor.read(MATH, edit)
    assert "lenght" in text
    # Build scripts, build-time tool sources and files without a grant stay refused.
    for rel in ("CMakeLists.txt", "tools/shadergen.cpp"):
        current, current_sha = (root / rel).read_text(), sha((root / rel).read_text())
        with pytest.raises(EditRefused):
            stack.editor.replace(rel, current + "// x\n", expected_sha256=current_sha, ctx=edit)
        assert (root / rel).read_text() == current
    no_grant = EditContext(operation=context, project_root=str(root))
    with pytest.raises(EditRefused):
        stack.editor.replace(MATH, text + "// x\n", expected_sha256=digest, ctx=no_grant)
    assert (root / MATH).read_text() == text
    stack.book.revoke(grant)
    with pytest.raises(EditRefused):
        stack.editor.replace(MATH, text + "// x\n", expected_sha256=digest, ctx=edit)


class _AnyFile:
    @staticmethod
    def allows(rel):
        return True, ""

    @staticmethod
    def digest():
        return "0" * 64


def _editor_on(stack, root, files):
    from sonder_runtime.application.build.grants import BuildFixGrantSpec, sha256_hex

    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    context = stack.context()
    spec = BuildFixGrantSpec(project_root=str(root), build_dir=str(root / "build"), target="t",
                             scope_digest="0" * 64, expires_at=time.time() + 60, scope=_AnyFile())
    digest = sha256_hex("editor-test")
    stack.book.approve(digest, context.principal_id)
    grant = stack.book.issue(spec, principal_id=context.principal_id,
                             job_id="build-fix-" + "8" * 32, plan_digest=digest)
    return EditContext(operation=context, project_root=str(root), job_id=grant.job_id,
                       grant_token=grant.token)


@pytest.mark.parametrize("before,after,tool", [
    (b"int a;\r\nint b;\r\n", "int a;\r\nint c;\r\n", "text_patch"),  # CRLF kept byte for byte
    (b"int a;\nint b;", "int a;\nint c;", "text_patch"),              # no newline at the end
    (b"int a;\nint b;\r\n", "int a;\nint c;\r\n", "write_file"),      # mixed endings
    (b"", "int fresh;\n", "text_patch"),                              # an empty file gains text
])
def test_the_gateway_editor_writes_exact_bytes(tmp_path, monkeypatch, before, after, tool):
    stack = Stack(tmp_path, monkeypatch)
    root = stack.allowed / "edit"
    edit = _editor_on(stack, root, {"src/x.cpp": before})
    text, digest = stack.editor.read("src/x.cpp", edit)
    assert text.encode() == before and digest == sha(text)
    receipt = stack.editor.replace("src/x.cpp", after, expected_sha256=digest, ctx=edit)
    assert (root / "src/x.cpp").read_bytes() == after.encode()
    assert receipt.after == sha(after) and receipt.tool == tool


def test_the_gateway_editor_detects_a_stale_expectation(tmp_path, monkeypatch):
    from sonder_runtime.application.build.fix_ports import EditConflict

    stack = Stack(tmp_path, monkeypatch)
    root = stack.allowed / "edit"
    edit = _editor_on(stack, root, {"src/x.cpp": b"int a;\n"})
    _, digest = stack.editor.read("src/x.cpp", edit)
    (root / "src/x.cpp").write_text("int changed;\n")
    with pytest.raises(EditConflict) as excinfo:
        stack.editor.replace("src/x.cpp", "int b;\n", expected_sha256=digest, ctx=edit)
    assert not excinfo.value.uncertain
    assert (root / "src/x.cpp").read_text() == "int changed;\n"
    with pytest.raises(EditRefused):
        stack.editor.read("../outside.cpp", edit)
