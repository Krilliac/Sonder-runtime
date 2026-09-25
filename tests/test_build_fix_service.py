"""The bounded build-fix loop over faithful fakes of its ports.

The build jobs are a deterministic fake: a file line containing ``ERR``
is a compile error at that line, ``LINKERR`` a linker error and ``WARN`` a
warning, so every build and ``compile_one`` outcome follows from the
editor's in-memory files. The editor is a fake ``SourceEditor`` with the
port's hash-checked semantics. Everything else is production code: the
domain policy (scope, patch validation, progress keys, reports), the
strategy bridge over the real controller, the durable job registry, the
cancellation tree, the pre-image store and the grant book.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("sonder_runtime.domain.build.repair", reason="needs lane A-domain-build")
pytest.importorskip("sonder_runtime.application.build.ports", reason="needs lane B1-run-and-model")

from sonder_runtime.adapters.build.preimages import FilePreimageStore  # noqa: E402
from sonder_runtime.application.build.fix_ports import (  # noqa: E402
    BuildFixRequest,
    EditConflict,
    EditReceipt,
    EditRefused,
    ResidencyRefused,
)
from sonder_runtime.application.build.fix_service import BuildFixService, measure  # noqa: E402
from sonder_runtime.application.build.grants import BuildFixGrantBook  # noqa: E402
from sonder_runtime.application.build.ports import BuildJobPlan  # noqa: E402
from sonder_runtime.application.build.strategy_bridge import StrategyFixAdapter  # noqa: E402
from sonder_runtime.application.cancellation_tree import CancellationTree  # noqa: E402
from sonder_runtime.application.context import local_owner_context  # noqa: E402
from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry  # noqa: E402
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus  # noqa: E402
from sonder_runtime.domain.build.attribution import Attribution  # noqa: E402
from sonder_runtime.domain.build.model import (  # noqa: E402
    BuildModel,
    BuildSystem,
    BuildTarget,
    CompileUnit,
    Generator,
    PchMode,
    TargetType,
)
from sonder_runtime.domain.build.repair import (  # noqa: E402
    CandidatePatch,
    FixStopReason,
    PatchHunk,
)
from sonder_runtime.domain.build.report import make_build_report  # noqa: E402
from sonder_runtime.domain.common.errors import SonderError  # noqa: E402
from sonder_runtime.domain.diagnostics.model import make_diagnostic  # noqa: E402

pytestmark = pytest.mark.unit


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ctx(principal: str = "owner"):
    context = local_owner_context(correlation_id=uuid.uuid4().hex)
    if principal != "owner":
        from dataclasses import replace

        context = replace(context, principal_id=principal)
    return context


# ---------------------------------------------------------------------------
# Fakes


class SyncThread:
    """Runs the worker inline: the loop is deterministic and finished on return."""

    def __init__(self, target, args=(), name="", daemon=True):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class FakeEditor:
    def __init__(self, files):
        self.files = files
        self.writes: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.contexts: list = []
        self.conflict_on_write = None
        self.refuse_writes = False

    def read(self, rel, edit_ctx):
        self.contexts.append(edit_ctx)
        if rel not in self.files:
            raise EditRefused("no such file", rel=rel, policy=False)
        self.reads.append(rel)
        return self.files[rel], sha(self.files[rel])

    def replace(self, rel, new_text, *, expected_sha256, ctx):
        self.contexts.append(ctx)
        if self.refuse_writes:
            raise EditRefused("refused by policy", rel=rel)
        if self.conflict_on_write is not None:
            raise EditConflict("changed underneath", uncertain=self.conflict_on_write, rel=rel)
        if sha(self.files[rel]) != expected_sha256:
            raise EditConflict("stale", uncertain=False, rel=rel)
        before = sha(self.files[rel])
        self.files[rel] = new_text
        self.writes.append((rel, new_text))
        return EditReceipt(rel=rel, before=before, after=sha(new_text),
                           receipt_id="r-%d" % len(self.writes),
                           effect_intent_id="run:intent-%d" % len(self.writes), tool="text_patch")


def diagnose(files, only=None):
    diags = []
    raw = 0
    for rel in sorted(files):
        if only is not None and rel != only:
            continue
        for number, line in enumerate(files[rel].splitlines(), start=1):
            raw += 1
            if "LINKERR" in line:
                diags.append(make_diagnostic(tool="gnu", severity="error", file="",
                                             message="undefined reference to `missing()'",
                                             raw_line_no=raw))
            elif "ERR" in line:
                diags.append(make_diagnostic(tool="gnu", severity="error", file=rel, line=number,
                                             col=1, message="'%s' was not declared" % line.strip()[:40],
                                             raw_line_no=raw))
            elif "WARN" in line:
                diags.append(make_diagnostic(tool="gnu", severity="warning", file=rel, line=number,
                                             col=1, message="unused variable", raw_line_no=raw))
    return diags


class FakeJobs:
    def __init__(self, harness):
        self.h = harness
        self.started: list[tuple[str, str, str]] = []
        self.cancelled: list[str] = []
        self.reports: dict[str, object] = {}
        self.block_builds = False
        self.reserved = []
        self.released = []
        self.compile_one_build_dir = None
        self.compile_one_fallback = False
        self.parents = []

    def plan(self, request, context, *, lease=None):
        action = request.action
        template = "cmake.build"
        target = request.target
        build_dir = self.h.build_dir
        if action == "compile_one":
            if self.compile_one_fallback:
                template = "cmake.build"
            else:
                template = "ninja.compile_one"
                build_dir = self.compile_one_build_dir or build_dir
            target = "core"
        if action == "build" and target not in {t.name for t in self.h.model.targets} | {"all"}:
            raise SonderError("unknown target")
        return BuildJobPlan(
            action=action, system="cmake", project_root=self.h.root, build_dir=build_dir,
            cwd=build_dir, argv=("cmake", "--build", build_dir), display_argv=("cmake", "--build"),
            cwd_label="proj", command_digest=sha("%s|%s|%s" % (action, target, request.file)),
            environment=(), env_keys=(), timeout_seconds=600, max_descendants=64,
            memory_limit_bytes=None, log_dir="", log_file="", binlog="", world="host",
            network="advisory_off", isolation_truth="unverified", model_digest="m",
            template_id=template, checked_executables=("cmake",), project_label="proj",
            target=target, file_label=request.file,
        )

    def reserve(self, build_dir, owner, context):
        self.reserved.append((build_dir, owner))
        return ("lease", owner)

    def release(self, lease):
        self.released.append(lease)

    def start(self, request, context, *, plan=None, lease=None, parent_job_id=""):
        child = "build-job-" + uuid.uuid4().hex
        self.started.append((child, request.action, request.file or request.target))
        self.parents.append(parent_job_id)
        if not self.block_builds:
            self.reports[child] = self._report(child, request)
        else:
            self.pending = (child, request)
        return child

    def _report(self, child, request, status=None):
        only = request.file if request.action == "compile_one" else None
        diags = diagnose(self.h.editor.files, only=only)
        errors = [d for d in diags if d.severity == "error"]
        warnings = [d for d in diags if d.severity == "warning"]
        atts = []
        for rel in sorted({d.file for d in diags if d.file}):
            mine = [d for d in diags if d.file == rel]
            first = next((d for d in mine if d.severity == "error"), None)
            atts.append(Attribution(kind="tu", label=rel, project="", first_error=first,
                                    error_count=sum(d.severity == "error" for d in mine),
                                    warning_count=sum(d.severity == "warning" for d in mine),
                                    diagnostics=tuple(mine)))
        status = status or ("failed" if errors else "succeeded")
        return make_build_report(
            status=status, job_id=child, action=request.action, system="cmake",
            target=request.target, first_errors=tuple(errors[:24]), attributions=tuple(atts),
            counts=(("error", len(errors)), ("warning", len(warnings))),
            exit_code=1 if errors else 0,
        )

    def result(self, child, context, *, wait_seconds=0):
        if child in self.reports:
            return self.reports[child]
        deadline = time.monotonic() + min(float(wait_seconds or 0), 2.0)
        while time.monotonic() < deadline and child not in self.reports:
            if context.cancellation.cancelled:
                break
            time.sleep(0.01)
        return self.reports.get(child, {"status": "running"})

    def cancel(self, child, context, *, reason="cancelled"):
        self.cancelled.append(child)
        if child not in self.reports:
            _, request = self.pending
            self.reports[child] = self._report(child, request, status="cancelled")
        return {"status": "cancelled"}


class FakeModels:
    def __init__(self, model):
        self._model = model

    def model(self, request, context):
        return self._model

    def cached_model(self, principal, project_root, build_dir):
        return self._model


class ScriptedGenerator:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def propose(self, evidence, context, *, route_hint=""):
        self.calls.append((evidence, route_hint))
        if not self.script:
            raise ValueError("the scripted generator has nothing left")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def patch(rel, anchor, replacement):
    return CandidatePatch(hunks=(PatchHunk(rel, anchor, replacement),), rationale="scripted",
                          model_id="scripted")


BASE_FILES = {
    "src/a.cpp": "int a() { return 1; }\nint x = ERR1;\nint y = ERR2;\n",
    "src/b.cpp": "int b() { return 2; }\n",
    "src/pch_user.cpp": "int p() { return 3; }\n",
    "src/unity_part.cpp": "int u() { return 4; }\n",
    "tools/gen.cpp": "int main() { return 0; }\n",
}


class Harness:
    def __init__(self, tmp_path, files=None, script=(), *, sync=True, grants=None,
                 propose_only_ok=False, unity=False):
        self.root = str(tmp_path / "proj")
        self.build_dir = self.root + "/build"
        self.model = BuildModel(
            project_label="proj", source_root=self.root, build_dir=self.build_dir,
            system=BuildSystem.CMAKE, generator=Generator.NINJA,
            targets=(
                BuildTarget("core", TargetType.STATIC_LIBRARY, unity=unity),
                BuildTarget("gen", TargetType.EXECUTABLE, build_time_tool=True),
            ),
            units=(
                CompileUnit("src/a.cpp", "src/a.cpp", target="core"),
                CompileUnit("src/b.cpp", "src/b.cpp", target="core"),
                CompileUnit("src/pch_user.cpp", "src/pch_user.cpp", target="core",
                            pch=PchMode.USE, pch_header="src/pch.h"),
                CompileUnit("src/unity_part.cpp", "src/unity_part.cpp", target="core"),
                CompileUnit("tools/gen.cpp", "tools/gen.cpp", target="gen"),
            ),
        )
        self.editor = FakeEditor(dict(files or BASE_FILES))
        self.originals = dict(self.editor.files)
        self.jobs = FakeJobs(self)
        self.generator = ScriptedGenerator(script)
        self.preimages = FilePreimageStore(tmp_path / "state" / "build-fix")
        self.registry = DurableJobRegistry()
        self.tree = CancellationTree()
        self.strategies: list[StrategyFixAdapter] = []
        self.grants = grants

        def strategy_factory():
            adapter = StrategyFixAdapter()
            self.strategies.append(adapter)
            return adapter

        self.service = BuildFixService(
            self.jobs, FakeModels(self.model), self.editor, self.generator, strategy_factory,
            None, self.preimages, self.registry, self.tree, clock=time.time,
            thread_factory=SyncThread if sync else None, grants=grants,
            propose_only_ok=propose_only_ok,
        )

    def run(self, **kwargs):
        request = BuildFixRequest(project=self.root, target=kwargs.pop("target", "core"), **kwargs)
        context = ctx()
        job = self.service.start(request, context)
        return job, self.service.result(job, context, wait_seconds=30)


# ---------------------------------------------------------------------------
# Tests


def test_improve_then_regress_then_revert_to_best(tmp_path):
    h = Harness(tmp_path, script=[
        patch("src/a.cpp", "int x = ERR1;", "int x = 1;"),
        patch("src/a.cpp", "int y = ERR2;", "int y = ERR2;\nint z = ERR3;"),
    ])
    job, report = h.run(attempts=2)
    assert report.stop_reason is FixStopReason.ATTEMPTS_EXHAUSTED
    assert report.status == "improved"
    assert [item.outcome for item in report.attempts] == ["improved", "regressed"]
    best_text = "int a() { return 1; }\nint x = 1;\nint y = ERR2;\n"
    assert h.editor.files["src/a.cpp"] == best_text  # the regression was reverted
    assert h.editor.writes[-1] == ("src/a.cpp", best_text)
    assert report.best.errors_total == 1 and report.initial.errors_total == 2
    assert report.files and report.files[0].after_sha256 == sha(best_text)
    assert report.preimage_label.endswith(job)
    assert h.preimages.load(job, "src/a.cpp") == (h.originals["src/a.cpp"], sha(h.originals["src/a.cpp"]))
    # Every attempt's writes carry the gateway's effect-intent ids.
    assert report.attempts[0].effect_intent_ids
    assert h.registry.poll(job).status is JobStatus.SUCCEEDED
    assert all(parent == job for parent in h.jobs.parents)


def test_a_fix_reaches_fixed_and_verifies_the_target(tmp_path):
    h = Harness(tmp_path, script=[
        patch("src/a.cpp", "int x = ERR1;", "int x = 1;"),
        patch("src/a.cpp", "int y = ERR2;", "int y = 2;"),
    ])
    job, report = h.run(attempts=4)
    assert report.status == "fixed" and report.stop_reason is FixStopReason.FIXED
    assert report.verification_scope == "target"
    assert report.final_build.status == "succeeded"
    assert "ERR" not in h.editor.files["src/a.cpp"]
    actions = [action for _, action, _ in h.jobs.started]
    assert actions[0] == "build" and "compile_one" in actions and actions[-1] == "build"


def test_two_attempts_without_progress_stop(tmp_path):
    h = Harness(tmp_path, script=[
        patch("src/a.cpp", "int a() { return 1; }", "int a() { return 1;  }"),
        patch("src/a.cpp", "int a() { return 1; }", "int a() {  return 1; }"),
        patch("src/a.cpp", "int x = ERR1;", "int x = 1;"),
    ])
    job, report = h.run(attempts=5)
    assert report.stop_reason is FixStopReason.NO_PROGRESS
    assert [item.outcome for item in report.attempts] == ["no_progress", "no_progress"]
    assert h.editor.files["src/a.cpp"] == h.originals["src/a.cpp"]
    assert len(h.generator.calls) == 2
    assert report.status == "unchanged"


def test_a_hostile_patch_is_rejected_without_a_write(tmp_path):
    h = Harness(tmp_path, script=[
        patch("src/a.cpp", "int x = ERR1;", '#include "/etc/shadow"\nint x = 1;'),
        patch("src/a.cpp", "int x = ERR1;", "%:include \"../../.env\"\nint x = 1;"),
    ])
    job, report = h.run(attempts=2)
    assert h.editor.writes == []
    assert [item.outcome for item in report.attempts] == ["rejected", "rejected"]
    assert any("HOSTILE_DIRECTIVE" in reason for reason in report.attempts[0].reasons)
    assert h.editor.files == h.originals


def test_an_edit_conflict_is_an_uncertain_side_effect_and_nothing_is_reverted(tmp_path):
    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    h.editor.conflict_on_write = True
    job, report = h.run(attempts=2)
    assert report.stop_reason is FixStopReason.UNCERTAIN_SIDE_EFFECT
    assert report.status == "aborted"
    assert h.editor.writes == []
    # The pre-image was saved before the write was attempted.
    assert [entry.rel for entry in h.preimages.list(job)] == ["src/a.cpp"]
    assert any("nothing was reverted" in note for note in report.notes)


def test_revert_after_restores_byte_identical_files(tmp_path):
    files = dict(BASE_FILES)
    files["src/a.cpp"] = "int a() { return 1; }\r\nint x = ERR1;\r\n"
    h = Harness(tmp_path, files=files, script=[
        patch("src/a.cpp", "int x = ERR1;", "int x = 1;"),
    ])
    job, report = h.run(attempts=2, revert_after=True)
    assert report.status == "fixed" and report.revert_after and report.applied
    assert h.editor.files == h.originals
    assert h.editor.files["src/a.cpp"].encode() == files["src/a.cpp"].encode()
    assert any("originals were restored" in note for note in report.notes)


def test_propose_only_is_refused_unless_the_operator_enables_it(tmp_path):
    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    with pytest.raises(SonderError) as excinfo:
        h.run(apply=False)
    assert excinfo.value.code == "FIX_SCOPE_REJECTED"
    assert h.jobs.started == [] and h.jobs.reserved == []
    allowed = Harness(tmp_path / "ok", script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")],
                      propose_only_ok=True)
    job, report = allowed.run(apply=False)
    assert allowed.editor.writes == [] and not report.applied
    assert report.files and "+int x = 1;" in report.files[0].diff
    assert [action for _, action, _ in allowed.jobs.started] == ["build"]  # baseline only


@pytest.mark.parametrize("focus,unity", [("src/pch_user.cpp", False), ("src/unity_part.cpp", True)])
def test_a_pch_or_unity_without_blob_focus_forces_a_target_build(tmp_path, focus, unity):
    files = dict(BASE_FILES)
    files["src/a.cpp"] = "int a() { return 1; }\n"
    files[focus] = files[focus] + "int q = ERR9;\n"
    h = Harness(tmp_path, files=files, unity=unity, script=[patch(focus, "int q = ERR9;", "int q = 9;")])
    job, report = h.run(attempts=2)
    assert report.status == "fixed"
    assert "compile_one" not in [action for _, action, _ in h.jobs.started]
    assert any("target build" in note for note in report.notes)


def test_a_linker_only_error_needs_a_build_script_change(tmp_path):
    files = dict(BASE_FILES)
    files["src/a.cpp"] = "int a() { return 1; } // LINKERR\n"
    h = Harness(tmp_path, files=files, script=[])
    job, report = h.run()
    assert report.stop_reason is FixStopReason.NEEDS_BUILD_SCRIPT_CHANGE
    assert h.generator.calls == [] and h.editor.writes == []


def test_a_focus_in_build_time_tool_sources_is_refused(tmp_path):
    files = dict(BASE_FILES)
    files["src/a.cpp"] = "int a() { return 1; }\n"
    files["tools/gen.cpp"] = "int main() { return ERR0; }\n"
    h = Harness(tmp_path, files=files, script=[])
    job, report = h.run(target="core")
    assert report.stop_reason is FixStopReason.BUILD_TIME_TOOL_SOURCE
    assert h.generator.calls == [] and h.editor.writes == []


def test_a_child_build_outside_the_grant_is_refused(tmp_path):
    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    h.jobs.compile_one_build_dir = h.root + "/elsewhere"
    job, report = h.run(attempts=2)
    assert report.stop_reason is FixStopReason.PERMISSION_DENIED
    assert all(action != "compile_one" for _, action, _ in h.jobs.started)


def test_residency_refusal_stops_before_any_write(tmp_path):
    h = Harness(tmp_path, script=[ResidencyRefused("route may leave the machine")])
    job, report = h.run()
    assert report.stop_reason is FixStopReason.RESIDENCY_REFUSED and report.status == "aborted"
    assert h.editor.writes == []


def test_a_write_refused_by_policy_is_permission_denied(tmp_path):
    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    h.editor.refuse_writes = True
    job, report = h.run()
    assert report.stop_reason is FixStopReason.PERMISSION_DENIED


def test_cancel_cascades_to_the_child_build(tmp_path):
    h = Harness(tmp_path, script=[], sync=False)
    h.jobs.block_builds = True
    request = BuildFixRequest(project=h.root, target="core")
    context = ctx()
    job = h.service.start(request, context)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not h.jobs.started:
        time.sleep(0.01)
    child = h.jobs.started[0][0]
    view = h.service.cancel(job, context, reason="operator stop")
    assert view.job_id == job
    report = h.service.result(job, context, wait_seconds=30)
    assert report.stop_reason is FixStopReason.CANCELLED
    assert child in h.jobs.cancelled
    assert h.registry.poll(job).status is JobStatus.CANCELLED
    assert h.jobs.released  # the build-dir reservation was given back


def test_a_foreign_principal_cannot_see_or_cancel_a_fix(tmp_path):
    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    job, _ = h.run(attempts=1)
    for call in (lambda c: h.service.result(job, c), lambda c: h.service.cancel(job, c),
                 lambda c: h.service.restore(job, c)):
        with pytest.raises(SonderError) as excinfo:
            call(ctx("mallory"))
        assert excinfo.value.code == "JOB_NOT_FOUND"


def test_the_grant_is_minted_for_an_approved_plan_and_revoked_at_the_end(tmp_path):
    book = BuildFixGrantBook(clock=time.time)
    h = Harness(tmp_path, grants=book, script=[
        patch("src/a.cpp", "int x = ERR1;", "int x = 1;"),
        patch("src/a.cpp", "int y = ERR2;", "int y = 2;"),
    ])
    request = BuildFixRequest(project=h.root, target="core")
    context = ctx()
    plan = h.service.plan(request, context)
    assert plan.resolved_command()["plan_digest"] == plan.plan_digest
    assert h.service.plan(request, context).plan_digest == plan.plan_digest  # stable re-plan
    book.approve(plan.plan_digest, context.principal_id)
    job = h.service.start(request, context)
    report = h.service.result(job, context)
    assert report.status == "fixed"
    tokens = {edit_ctx.grant_token for edit_ctx in h.editor.contexts}
    assert len(tokens) == 1 and "" not in tokens
    assert book.live() == 0 and book.lookup(tokens.pop()) is None


def test_without_an_approval_no_grant_is_minted(tmp_path):
    book = BuildFixGrantBook(clock=time.time)
    h = Harness(tmp_path, grants=book, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    job, report = h.run(attempts=1)
    assert {edit_ctx.grant_token for edit_ctx in h.editor.contexts} == {""}
    assert any("no fix grant was minted" in note for note in report.notes)


def _crashed_job(h, rel, new_text):
    """The durable state a crash mid-fix leaves: running manifest, pre-image, edited file."""
    job = "build-fix-" + uuid.uuid4().hex
    h.registry.start(JobIdentity(job, "tool.build_fix", job, job), max_attempts=1,
                     metadata={"principal_id": "owner"})
    h.registry.transition(job, JobStatus.RUNNING)
    h.preimages.begin(job, {"principal_id": "owner", "project_root": h.root, "target": "core",
                            "status": "running"})
    original = h.editor.files[rel]
    h.preimages.save(job, rel, original, sha(original))
    h.editor.files[rel] = new_text
    h.preimages.record_write(job, rel, sha(new_text))
    return job


def test_recovery_marks_interrupted_refuses_retry_and_restores(tmp_path):
    h = Harness(tmp_path)
    original = h.editor.files["src/a.cpp"]
    job = _crashed_job(h, "src/a.cpp", "int a() { return 1; }\nint x = 1;\nint y = ERR2;\n")
    assert h.service.recover() == (job,)
    assert h.preimages.manifest(job)["status"] == "interrupted"
    assert h.registry.poll(job).status is JobStatus.INTERRUPTED
    with pytest.raises(ValueError):
        h.registry.retry(job)  # retry_budget = 0: an interrupted fix is never replayed
    shown = h.service.result(job, ctx())
    assert shown["status"] == "interrupted" and shown["preimage_label"].endswith(job)
    row = shown["files"][0]
    assert row["original_sha256"] == sha(original)
    assert row["current_sha256"] == row["last_written_sha256"] != row["original_sha256"]
    restored = h.service.restore(job, ctx())
    assert restored["restored"] == ["src/a.cpp"]
    assert h.editor.files["src/a.cpp"] == original


def test_restore_conflict_when_the_file_changed_after_the_fix(tmp_path):
    h = Harness(tmp_path)
    job = _crashed_job(h, "src/a.cpp", "int fixed = 1;\n")
    h.service.recover()
    h.editor.files["src/a.cpp"] = "int someone_else_edited = 1;\n"
    with pytest.raises(SonderError) as excinfo:
        h.service.restore(job, ctx())
    assert excinfo.value.code == "RESTORE_CONFLICT"
    assert h.editor.files["src/a.cpp"] == "int someone_else_edited = 1;\n"


def test_a_tampered_preimage_is_never_written_back(tmp_path):
    h = Harness(tmp_path)
    job = _crashed_job(h, "src/a.cpp", "int fixed = 1;\n")
    blob = next((tmp_path / "state" / "build-fix" / job / "blobs").iterdir())
    blob.write_text("#include \"/etc/shadow\"\n")
    with pytest.raises(SonderError):
        h.service.restore(job, ctx())
    assert h.editor.files["src/a.cpp"] == "int fixed = 1;\n"


def test_measure_never_reads_a_failed_build_as_fixed():
    report = make_build_report(status="failed", job_id="build-job-" + "0" * 16, action="build",
                               system="cmake", counts=(), exit_code=2)
    progress = measure(report, focus="", edited=frozenset(), baseline_warnings={})
    assert progress.build_ran and not progress.fixed and progress.failed_units == 1
    skipped = make_build_report(status="did_not_run", job_id="build-job-" + "1" * 16,
                                action="build", system="cmake")
    assert not measure(skipped, focus="", edited=frozenset(), baseline_warnings={}).build_ran


def test_threads_come_from_the_injected_factory(tmp_path):
    started = []

    class Recording(SyncThread):
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name"))
            super().__init__(*args, **kwargs)

    h = Harness(tmp_path, script=[patch("src/a.cpp", "int x = ERR1;", "int x = 1;")])
    h.service._thread_factory = Recording
    job, _ = h.run(attempts=1)
    assert started == ["build-fix-%s" % job[-8:]]
    assert threading.active_count() >= 1
