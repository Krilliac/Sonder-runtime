"""The ``build-fix`` effect family: journaled fix edits, crash cuts and proofs.

A build fix's source edits are journaled in the worker effect journal: the
intent before the edit, the receipt after it. After a crash, startup
reconciliation offers each unresolved edit to ``BuildFixEditVerifier``,
which proves it from the edited file's current SHA-256 against the
journaled before/after digests, or leaves it fenced.

The crash cases run the real ``BuildFixService`` loop (over the faithful
fakes of ``test_build_fix_service``, with an editor that edits real files)
in a child interpreter against a file-backed ``SQLiteEffectJournal``, and
kill it with ``os._exit`` at one cut, so no ``except``/``finally`` handler
runs. Every real file write is appended to a counter file, so a second
application of an edit is counted, not inferred. The child's exit status is
asserted first: a case whose cut never fired fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sonder_runtime.adapters.build.fix_effect_verifier import (  # noqa: E402
    MAX_FILE_BYTES,
    BuildFixEditVerifier,
)
from sonder_runtime.adapters.persistence.sqlite.effect_journal import (  # noqa: E402
    SQLiteEffectJournal,
)
from sonder_runtime.application.build.fix_effects import (  # noqa: E402
    BuildFixEdit,
    BuildFixEffects,
    edit_from_intent,
    run_id_for,
)
from sonder_runtime.application.build.fix_ports import EditConflict  # noqa: E402
from sonder_runtime.application.build.fix_service import BuildFixService  # noqa: E402
from sonder_runtime.application.build.strategy_bridge import StrategyFixAdapter  # noqa: E402
from sonder_runtime.application.execution.effect_journal import (  # noqa: E402
    EffectIntent,
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.effect_reconciliation import (  # noqa: E402
    reconcile_unresolved_effects,
)
from sonder_runtime.application.execution.worker_bindings import (  # noqa: E402
    AuthenticatedWorkerBinding,
    effect_request_digest,
)
from sonder_runtime.domain.build.repair import FixStopReason  # noqa: E402
from sonder_runtime.application.ports.jobs import JobStatus  # noqa: E402
from sonder_runtime.domain.common.errors import (  # noqa: E402
    Cancelled,
    DeadlineExceeded,
    SonderError,
)
from tests.test_build_fix_service import (  # noqa: E402
    BASE_FILES,
    FakeEditor,
    FakeModels,
    Harness,
    SyncThread,
    ctx,
    patch,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
CRASH_EXIT = 87
WORKER = "build-fix:crash-node"
REL = "src/a.cpp"
ORIGINAL = BASE_FILES[REL]
IMPROVED = "int a() { return 1; }\nint x = 1;\nint y = ERR2;\n"
REGRESSED = "int a() { return 1; }\nint x = 1;\nint y = ERR2;\nint z = ERR3;\n"
SCRIPT = (
    ("int x = ERR1;", "int x = 1;"),
    ("int y = ERR2;", "int y = ERR2;\nint z = ERR3;"),
)
# (which real edit to cut: 1 = attempt 1's write, 3 = the revert of attempt 2;
#  where: before the file write, or after it and before the editor returns)
CUTS = {
    "write_after_intent": (1, "before_write"),
    "write_after_edit": (1, "after_write"),
    "revert_after_edit": (3, "after_write"),
}
# Only for the fence test: attempt 2's write, cut before it reaches the file.
FENCE_CUT = {"second_write_after_intent": (2, "before_write")}
# A fix with ``revert_after``: its 4th edit restores the original, cut after it.
REVERT_AFTER_CUT = {"revert_after_after_edit": (4, "after_write")}
ALL_CUTS = {**CUTS, **FENCE_CUT, **REVERT_AFTER_CUT}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DiskFiles:
    """The fake editor's ``files`` mapping, backed by real files under ``root``."""

    def __init__(self, root: Path, counter: Path) -> None:
        self.root = root
        self.counter = counter

    def _path(self, rel: str) -> Path:
        return self.root / rel

    def __contains__(self, rel) -> bool:
        return isinstance(rel, str) and self._path(rel).is_file()

    def __getitem__(self, rel: str) -> str:
        return self._path(rel).read_bytes().decode("utf-8")

    def __setitem__(self, rel: str, text: str) -> None:
        with self._path(rel).open("wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        with self.counter.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps([rel, sha(text)]) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def __iter__(self):
        return iter(sorted(str(path.relative_to(self.root)).replace(os.sep, "/")
                           for path in self.root.rglob("*.cpp")))

    def keys(self):
        return list(iter(self))

    def items(self):
        return [(rel, self[rel]) for rel in self]


class CrashEditor(FakeEditor):
    """The fake editor over real files, killing the interpreter at one cut."""

    def __init__(self, files, cut=None):
        super().__init__(files)
        self.cut = cut
        self.calls = 0

    def replace(self, rel, new_text, *, expected_sha256, ctx):
        self.calls += 1
        armed = self.cut is not None and self.calls == self.cut[0]
        if armed and self.cut[1] == "before_write":
            os._exit(CRASH_EXIT)
        receipt = super().replace(rel, new_text, expected_sha256=expected_sha256, ctx=ctx)
        if armed and self.cut[1] == "after_write":
            os._exit(CRASH_EXIT)
        return receipt


def seed(root: Path) -> None:
    for rel, text in BASE_FILES.items():
        path = root / "proj" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


def journal_at(root: Path, *, verifier: bool = False, db: Path | None = None
               ) -> SQLiteEffectJournal:
    return SQLiteEffectJournal(
        db or root / "worker-effects.db",
        reconciliation_verifiers={"build-fix": BuildFixEditVerifier()} if verifier else None,
    )


def factory(journal, epoch: int, worker: str = WORKER):
    def make(run_id: str, scope: str) -> AuthenticatedWorkerBinding:
        return AuthenticatedWorkerBinding(journal, run_id, worker, epoch, scope,
                                          auto_reconcile=True)
    return make


def _config(root: Path):
    from dataclasses import replace

    from sonder_runtime.platform.config import SonderConfig

    config = SonderConfig()
    return replace(config, state=replace(
        config.state, home=str(root / "state"), workspace_roots=(str(root / "workspace"),),
    ))


def fix_service(root: Path, journal, epoch: int, *, cut=None, script=SCRIPT,
                worker: str = WORKER):
    """A ``BuildFixService`` over the fakes, editing real files, journaling edits."""
    h = Harness(root, script=[patch(REL, anchor, text) for anchor, text in script])
    h.editor = CrashEditor(DiskFiles(Path(h.root), root / "writes.log"), cut)
    h.service = BuildFixService(
        h.jobs, FakeModels(h.model), h.editor, h.generator, lambda: StrategyFixAdapter(), None,
        h.preimages, h.registry, h.tree, clock=time.time, thread_factory=SyncThread,
        effect_binding_factory=factory(journal, epoch, worker),
    )
    return h


def writes(root: Path) -> list[list[str]]:
    log = root / "writes.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _child(case: str, root: Path, composed: bool) -> None:
    if composed:
        # The production journal file and this host's build-fix worker identity.
        worker = "build-fix:" + _config(root).compute.node_id
        journal = journal_at(root, db=root / "state" / "worker-effects.db")
    else:
        worker, journal = WORKER, journal_at(root)
    h = fix_service(root, journal, 1, cut=ALL_CUTS[case], worker=worker)
    job, _report = h.run(attempts=2, revert_after=case in REVERT_AFTER_CUT)
    (root / "job.txt").write_text(job, encoding="utf-8")
    os._exit(0)  # the cut never fired: the parent fails the case


def crash(case: str, root: Path, *, composed: bool = False) -> str:
    seed(root)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), env.get("PYTHONPATH"))))
    env["SONDER_STATE_HOME"] = str(root / "state-home")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case, str(root),
         "composed" if composed else "bare"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == CRASH_EXIT, (completed.returncode, completed.stderr[-4000:])
    jobs = [path.name for path in (root / "state" / "build-fix").iterdir() if path.is_dir()]
    assert len(jobs) == 1, jobs
    return jobs[0]


# ---------------------------------------------------------------------------
# Hard-crash cuts, then startup reconciliation


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
@pytest.mark.parametrize("case", sorted(CUTS))
def test_a_crashed_fix_edit_is_proven_at_startup_and_never_applied_twice(case, tmp_path):
    job = crash(case, tmp_path)
    run_id = run_id_for(job)
    target = tmp_path / "proj" / REL
    expected_file = {"write_after_intent": ORIGINAL, "write_after_edit": IMPROVED,
                     "revert_after_edit": IMPROVED}[case]
    expected_writes = {"write_after_intent": 0, "write_after_edit": 1, "revert_after_edit": 3}[case]
    assert target.read_text() == expected_file
    assert len(writes(tmp_path)) == expected_writes

    # On disk: exactly one bare intent for the cut edit, admitted before it ran.
    raw = journal_at(tmp_path)
    page = raw.effects_since(run_id, 0, limit=50)
    unresolved = page.unresolved
    assert len(unresolved) == 1 and unresolved[0].state is EffectState.INTENT
    intent = unresolved[0]
    assert intent.operation_id.startswith("build-fix:") and intent.receipt_key == ""
    edit = edit_from_intent(intent)
    assert edit is not None and edit.job_id == job and edit.rel == REL
    settled_before = [record for record in page.records if record is not intent]
    assert all(record.state is EffectState.COMPLETED for record in settled_before)
    if case == "revert_after_edit":
        assert (edit.candidate, edit.before_sha256, edit.after_sha256) == (
            "revert-2", sha(REGRESSED), sha(IMPROVED))
    else:
        assert (edit.candidate, edit.before_sha256, edit.after_sha256) == (
            "attempt-1", sha(ORIGINAL), sha(IMPROVED))

    # Startup: the host (a newer epoch) reconciles with the trusted verifier.
    journal = journal_at(tmp_path, verifier=True)
    report = reconcile_unresolved_effects(journal, owner_epoch=2,
                                          owns_worker={WORKER}.__contains__)
    assert not report.fenced and not report.foreign_runs and not report.failed_runs
    assert [item.intent_id for item in report.resolved] == [intent.intent_id]
    proven = journal.get(intent.intent_id)
    applied = case != "write_after_intent"
    assert proven.state is (EffectState.COMPLETED if applied else EffectState.FAILED)
    assert proven.detail == "verified:build-fix-source-sha256-v1:%s:sha256:%s" % (
        "applied" if applied else "not-applied", sha(expected_file))
    # Proving read the file; it changed nothing.
    assert target.read_text() == expected_file
    assert len(writes(tmp_path)) == expected_writes
    # Re-entrant: a second pass finds nothing to do.
    assert reconcile_unresolved_effects(journal, owner_epoch=2,
                                        owns_worker={WORKER}.__contains__).resolved == ()

    # A restarted fix asking for the same edit is refused before the editor runs.
    restarted = fix_service(tmp_path, journal, 3)
    effects = BuildFixEffects(factory(journal, 3))
    text = IMPROVED
    with pytest.raises(EditConflict) as refused:
        effects.replace(restarted.editor, job_id=job, candidate=edit.candidate, rel=REL,
                        text=text, before_sha256=edit.before_sha256,
                        after_sha256=edit.after_sha256,
                        ctx=_edit_ctx(tmp_path, job))
    assert refused.value.uncertain is False and "journal refused" in str(refused.value)
    assert restarted.editor.calls == 0
    assert len(writes(tmp_path)) == expected_writes
    assert target.read_text() == expected_file

    # build_fix_restore from the restarted runtime: a proven edit counts as the
    # fix's own write even though the crash came before its pre-image record.
    context = ctx()
    restored = restarted.service.restore(job, context)
    if expected_file == ORIGINAL:
        assert restored["already_original"] == [REL] and restored["restored"] == []
    else:
        assert restored["restored"] == [REL]
    assert target.read_text() == ORIGINAL
    records = journal.effects_since(run_id, 0, limit=50).records
    assert not [record for record in records
                if record.state in {EffectState.INTENT, EffectState.UNCERTAIN}]


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_an_edit_the_file_no_longer_shows_stays_fenced(tmp_path):
    job = crash("write_after_edit", tmp_path)
    target = tmp_path / "proj" / REL
    # Someone changed the file after the crash: neither digest matches.
    target.write_text(IMPROVED + "// hand edit\n")
    journal = journal_at(tmp_path, verifier=True)
    report = reconcile_unresolved_effects(journal, owner_epoch=2,
                                          owns_worker={WORKER}.__contains__)
    assert report.resolved == () and len(report.fenced) == 1
    intent_id = report.fenced[0].intent_id
    assert journal.get(intent_id).state is EffectState.UNCERTAIN
    # The run stays fenced: no successor may edit, and a restore refuses.
    successor = AuthenticatedWorkerBinding(journal, run_id_for(job), WORKER, 3, str(tmp_path),
                                           auto_reconcile=True)
    with pytest.raises(EffectJournalError, match="reconciliation"):
        successor.recover_before_restart()
    restarted = fix_service(tmp_path, journal, 4)
    with pytest.raises(SonderError):
        restarted.service.restore(job, ctx())
    assert restarted.editor.calls == 0
    assert target.read_text() == IMPROVED + "// hand edit\n"
    # Put back to the digest the edit wrote: the next pass proves it.
    target.write_text(IMPROVED)
    again = reconcile_unresolved_effects(journal, owner_epoch=5,
                                         owns_worker={WORKER}.__contains__)
    assert [item.intent_id for item in again.resolved] == [intent_id]
    assert journal.get(intent_id).state is EffectState.COMPLETED


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_restore_refuses_to_write_over_an_unproven_edit(tmp_path):
    """The file still carries the pre-image record's digest, so only the
    journal's fence stops the restore; with the verifier the same restore runs."""
    job = crash("second_write_after_intent", tmp_path)
    target = tmp_path / "proj" / REL
    assert target.read_text() == IMPROVED and len(writes(tmp_path)) == 1
    unproven = journal_at(tmp_path)  # no verifier: the edit cannot be proven
    restarted = fix_service(tmp_path, unproven, 2)
    with pytest.raises(SonderError, match="could not be proven"):
        restarted.service.restore(job, ctx())
    assert restarted.editor.calls == 0 and target.read_text() == IMPROVED
    proving = journal_at(tmp_path, verifier=True)
    restarted = fix_service(tmp_path, proving, 3)
    assert restarted.service.restore(job, ctx())["restored"] == [REL]
    assert target.read_text() == ORIGINAL
    records = proving.effects_since(run_id_for(job), 0, limit=50).records
    assert [record.state for record in records] == [
        EffectState.COMPLETED, EffectState.FAILED, EffectState.COMPLETED]


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_a_foreign_hosts_fix_edit_is_left_alone(tmp_path):
    crash("write_after_edit", tmp_path)
    journal = journal_at(tmp_path, verifier=True)
    report = reconcile_unresolved_effects(journal, owner_epoch=2,
                                          owns_worker={"build-fix:other-node"}.__contains__)
    assert report.resolved == () and len(report.foreign_runs) == 1


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_a_crashed_revert_after_edit_is_journaled_and_proven(tmp_path):
    """``revert_after`` restores the originals through the journal too."""
    job = crash("revert_after_after_edit", tmp_path)
    target = tmp_path / "proj" / REL
    assert target.read_text() == ORIGINAL and len(writes(tmp_path)) == 4
    raw = journal_at(tmp_path)
    unresolved = raw.effects_since(run_id_for(job), 0, limit=50).unresolved
    assert len(unresolved) == 1
    edit = edit_from_intent(unresolved[0])
    assert (edit.candidate, edit.before_sha256, edit.after_sha256) == (
        "revert-2", sha(IMPROVED), sha(ORIGINAL))
    journal = journal_at(tmp_path, verifier=True)
    report = reconcile_unresolved_effects(journal, owner_epoch=2,
                                          owns_worker={WORKER}.__contains__)
    assert [item.intent_id for item in report.resolved] == [unresolved[0].intent_id]
    assert journal.get(unresolved[0].intent_id).state is EffectState.COMPLETED
    restarted = fix_service(tmp_path, journal, 3)
    assert restarted.service.restore(job, ctx())["already_original"] == [REL]
    assert restarted.editor.calls == 0 and len(writes(tmp_path)) == 4


@pytest.mark.parametrize("stop", [Cancelled, DeadlineExceeded])
@pytest.mark.parametrize("verifier", [True, False])
def test_an_edit_interrupted_in_the_gateway_fences_the_run_until_proven(tmp_path, stop, verifier):
    """A cancellation raised by the editor leaves its edit uncertain. With the
    verifier the fix proves it from the file and ``revert_after`` still
    restores the originals; without one every later edit is refused."""
    seed(tmp_path)
    journal = journal_at(tmp_path, verifier=verifier)
    h = fix_service(tmp_path, journal, 1)
    real = h.editor.replace
    calls = []

    def replace(rel, text, *, expected_sha256, ctx):
        calls.append(rel)
        if len(calls) == 2:  # attempt 2's write, before it reaches the file
            raise stop("stopped inside the gateway")
        return real(rel, text, expected_sha256=expected_sha256, ctx=ctx)

    h.editor.replace = replace
    job, report = h.run(attempts=4, revert_after=True)
    records = journal.effects_since(run_id_for(job), 0, limit=50).records
    states = [(record.state, edit_from_intent(record).candidate) for record in records]
    target = tmp_path / "proj" / REL
    if verifier:
        assert states == [(EffectState.COMPLETED, "attempt-1"), (EffectState.FAILED, "attempt-2"),
                          (EffectState.COMPLETED, "revert-2")]
        assert target.read_text() == ORIGINAL and len(writes(tmp_path)) == 2
        assert any("originals were restored" in note for note in report.notes)
    else:
        assert states == [(EffectState.COMPLETED, "attempt-1"),
                          (EffectState.UNCERTAIN, "attempt-2")]
        assert target.read_text() == IMPROVED and len(writes(tmp_path)) == 1
        assert any("revert_after failed" in note for note in report.notes)
    assert len(calls) == len(writes(tmp_path)) + 1


def _edit_ctx(root: Path, job: str):
    from sonder_runtime.application.build.fix_ports import EditContext

    return EditContext(operation=ctx(), project_root=str(root / "proj"), job_id=job)


# ---------------------------------------------------------------------------
# The live loop journals every edit


def test_every_edit_of_a_fix_is_a_settled_build_fix_effect(tmp_path):
    seed(tmp_path)
    journal = journal_at(tmp_path, verifier=True)
    h = fix_service(tmp_path, journal, 1)
    job, report = h.run(attempts=2)
    assert [item.outcome for item in report.attempts] == ["improved", "regressed"]
    records = journal.effects_since(run_id_for(job), 0, limit=50).records
    edits = [edit_from_intent(record) for record in records]
    assert [(edit.candidate, edit.before_sha256, edit.after_sha256) for edit in edits] == [
        ("attempt-1", sha(ORIGINAL), sha(IMPROVED)),
        ("attempt-2", sha(IMPROVED), sha(REGRESSED)),
        ("revert-2", sha(REGRESSED), sha(IMPROVED)),
    ]
    assert all(record.state is EffectState.COMPLETED for record in records)
    assert all(record.worker_id == WORKER and record.scope == str(tmp_path / "proj")
               for record in records)
    # The attempts name the journaled intents.
    named = [intent for item in report.attempts for intent in item.effect_intent_ids]
    assert named == [records[0].intent_id, records[1].intent_id]
    # Every write reached the file exactly once.
    assert [entry[1] for entry in writes(tmp_path)] == [sha(IMPROVED), sha(REGRESSED),
                                                       sha(IMPROVED)]
    # A restore in the same runtime journals its edit too.
    restored = h.service.restore(job, ctx())
    assert restored["restored"] == [REL] and (tmp_path / "proj" / REL).read_text() == ORIGINAL
    last = journal.effects_since(run_id_for(job), 0, limit=50).records[-1]
    assert last.state is EffectState.COMPLETED
    assert edit_from_intent(last).candidate.startswith("restore-")


def test_an_uncertain_edit_is_journaled_uncertain_and_later_proven(tmp_path):
    seed(tmp_path)
    journal = journal_at(tmp_path, verifier=True)
    h = fix_service(tmp_path, journal, 1)
    h.editor.conflict_on_write = True  # the gateway cannot prove the write
    job, report = h.run(attempts=2)
    assert report.stop_reason is FixStopReason.UNCERTAIN_SIDE_EFFECT
    (record,) = journal.effects_since(run_id_for(job), 0, limit=50).records
    assert record.state is EffectState.UNCERTAIN
    # The file never changed: the restore's pre-resume reconciliation proves
    # the edit was not applied and finds nothing to write.
    h.editor.conflict_on_write = None
    restored = h.service.restore(job, ctx())
    assert restored["already_original"] == [REL]
    assert journal.get(record.intent_id).state is EffectState.FAILED


def test_a_refused_or_stale_edit_is_a_failed_effect(tmp_path):
    seed(tmp_path)
    journal = journal_at(tmp_path)
    h = fix_service(tmp_path, journal, 1)
    h.editor.refuse_writes = True
    job, report = h.run(attempts=2)
    assert report.stop_reason is FixStopReason.PERMISSION_DENIED
    (record,) = journal.effects_since(run_id_for(job), 0, limit=50).records
    assert record.state is EffectState.FAILED and ":not-applied:EditRefused" in record.receipt_key
    assert writes(tmp_path) == []


def test_no_journal_means_no_build_fix_effects(tmp_path):
    seed(tmp_path)
    h = fix_service(tmp_path, journal_at(tmp_path), 1)
    h.service._effects = None
    job, report = h.run(attempts=2)
    assert report.attempts[0].effect_intent_ids == ("run:intent-1",)
    assert journal_at(tmp_path).effects_since(run_id_for(job), 0, limit=5).records == ()


# ---------------------------------------------------------------------------
# The verifier and the intent identity


JOB = "build-fix-" + "a" * 32


def _intent(root: Path, rel: str, before: str, after: str, **changes) -> EffectIntent:
    edit = BuildFixEdit(job_id=JOB, candidate="attempt-1", rel=rel, before_sha256=before,
                        after_sha256=after, project_root=str(root))
    fields = dict(
        intent_id="%s:%s" % (edit.run_id, edit.operation_id), run_id=edit.run_id,
        worker_id=WORKER, operation_id=edit.operation_id, scope=str(root), owner_epoch=1,
        idempotency_key=edit.idempotency_key,
        request_digest=effect_request_digest(edit.request()), reconciliation="query",
    )
    fields.update(changes)
    return EffectIntent(**fields)


def test_the_verifier_proves_from_the_current_digest_only(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    path = root / "src" / "a.cpp"
    verifier = BuildFixEditVerifier()
    path.write_text("after\n")
    proof = verifier.verify(_intent(root, "src/a.cpp", sha("before\n"), sha("after\n")))
    assert proof.state is EffectState.COMPLETED and proof.external_reference.startswith("applied:")
    path.write_text("before\n")
    proof = verifier.verify(_intent(root, "src/a.cpp", sha("before\n"), sha("after\n")))
    assert proof.state is EffectState.FAILED and proof.external_reference.startswith("not-applied:")
    path.write_text("neither\n")
    assert verifier.verify(_intent(root, "src/a.cpp", sha("before\n"), sha("after\n"))) is None
    # Missing file: no proof.
    assert verifier.verify(_intent(root, "src/b.cpp", sha("before\n"), sha("after\n"))) is None


def test_the_verifier_refuses_links_escapes_oversize_and_tampered_rows(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.cpp").write_text("after\n")
    verifier = BuildFixEditVerifier()
    before, after = sha("before\n"), sha("after\n")
    # A final link is never followed.
    (root / "src" / "link.cpp").symlink_to(outside / "x.cpp")
    assert verifier.verify(_intent(root, "src/link.cpp", before, after)) is None
    # A linked parent that leaves the root is refused.
    (root / "esc").symlink_to(outside)
    assert verifier.verify(_intent(root, "esc/x.cpp", before, after)) is None
    # Oversized files are not read.
    big = "a" * (MAX_FILE_BYTES + 1)
    (root / "src" / "big.cpp").write_text(big)
    assert verifier.verify(_intent(root, "src/big.cpp", before, sha(big))) is None
    # Rows that do not round-trip are not build-fix edits.
    (root / "src" / "a.cpp").write_text("after\n")
    good = _intent(root, "src/a.cpp", before, after)
    assert verifier.verify(good) is not None
    for changes in (
        {"request_digest": "0" * 64},
        {"scope": str(outside)},
        {"run_id": "build-fix:" + "build-fix-" + "b" * 32},
        {"worker_id": "process:crash-node"},
        {"operation_id": "build-fix:" + "0" * 40},
        {"idempotency_key": good.idempotency_key.replace("attempt-1", "attempt-2")},
    ):
        assert verifier.verify(_intent(root, "src/a.cpp", before, after, **changes)) is None


def test_the_edit_identity_is_validated():
    ok = dict(job_id=JOB, candidate="attempt-1", rel="src/a.cpp", before_sha256="a" * 64,
              after_sha256="b" * 64, project_root="/p")
    BuildFixEdit(**ok)
    for bad in ({"job_id": "job"}, {"candidate": "../x"}, {"rel": "../etc/passwd"},
                {"rel": "/etc/passwd"}, {"rel": "src/./a.cpp"}, {"before_sha256": "A" * 64},
                {"after_sha256": "a" * 64}, {"project_root": ""}):
        with pytest.raises(ValueError):
            BuildFixEdit(**{**ok, **bad})
    edit = BuildFixEdit(**ok)
    assert edit.idempotency_key == json.dumps(
        ["build-fix", JOB, "attempt-1", "src/a.cpp", "a" * 64, "b" * 64], separators=(",", ":"))
    assert edit.operation_id.startswith("build-fix:") and len(edit.operation_id) == 50


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_build_application_proves_a_crashed_fix_edit_at_startup(tmp_path, monkeypatch):
    """Production composition: the verifier is registered, the family is owned,
    and the fix service is composed over the host's build-fix worker binding."""
    from sonder_runtime.bootstrap import build_tools
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    job = crash("write_after_edit", tmp_path, composed=True)
    database = tmp_path / "state" / "worker-effects.db"
    (intent,) = journal_at(tmp_path, db=database).effects_since(run_id_for(job), 0).unresolved
    assert intent.state is EffectState.INTENT

    composed = {}
    original = build_tools.compose_build_tools

    def capture(**kwargs):
        composed.update(kwargs)
        services = original(**kwargs)
        composed["services"] = services
        return services

    monkeypatch.setattr(build_tools, "compose_build_tools", capture)
    application = build_application(config=_config(tmp_path))
    try:
        proven = journal_at(tmp_path, db=database).get(intent.intent_id)
        assert proven.state is EffectState.COMPLETED
        assert proven.detail.startswith("verified:build-fix-source-sha256-v1:applied:")
        assert application.worker_effect_reconciliation().fenced == ()
        assert (tmp_path / "proj" / REL).read_text() == IMPROVED
        assert len(writes(tmp_path)) == 1
        # The composed fix journals through this host's build-fix binding.
        fix = composed["services"].fix
        assert fix is not None
        binding = fix._effects.binding(job, str(tmp_path / "proj"))
        assert binding.worker_id == intent.worker_id and binding.auto_reconcile
        assert Path(binding.journal.database_path) == database
    finally:
        application.close_delegation(timeout=10)


@pytest.mark.skipif(os.name != "posix", reason="real POSIX process crash cut")
def test_build_application_marks_a_crashed_fix_interrupted(tmp_path, monkeypatch):
    """Production composition calls ``BuildFixService.recover()`` after the
    journal proof: the crashed fix reads ``interrupted`` instead of ``running``,
    the proven edit is neither retried nor reverted, the restore still works,
    and a second composition finds nothing left to recover."""
    from sonder_runtime.bootstrap import app as app_module
    from sonder_runtime.bootstrap import build_tools
    from sonder_runtime.bootstrap.app import build_application

    (tmp_path / "workspace").mkdir()
    job = crash("write_after_edit", tmp_path, composed=True)
    manifest = tmp_path / "state" / "build-fix" / job / "manifest.json"
    assert json.loads(manifest.read_text())["status"] == "running"

    composed = {}
    original = build_tools.compose_build_tools

    def capture(**kwargs):
        services = original(**kwargs)
        composed["services"] = services
        composed["job_registry"] = kwargs["job_registry"]
        return services

    recovered = []
    real_recover = app_module._recover_interrupted_build_fixes

    def observe(services):
        result = real_recover(services)
        recovered.append(result)
        return result

    monkeypatch.setattr(build_tools, "compose_build_tools", capture)
    monkeypatch.setattr(app_module, "_recover_interrupted_build_fixes", observe)
    application = build_application(config=_config(tmp_path))
    try:
        assert recovered == [(job,)]
        fix = composed["services"].fix
        assert json.loads(manifest.read_text())["status"] == "interrupted"
        assert fix.status(job, ctx()).status == "interrupted"
        shown = fix.result(job, ctx())
        assert shown["status"] == "interrupted"
        # The crashed child ran over an in-process registry, so the durable
        # registry either never saw the job or now holds it interrupted.
        registry = composed["job_registry"]()
        try:
            record = registry.poll(job)
        except KeyError:
            pass
        else:
            assert record.status is JobStatus.INTERRUPTED
        # Recovery rewrote status only: the proven edit is still on disk, once.
        assert (tmp_path / "proj" / REL).read_text() == IMPROVED
        assert len(writes(tmp_path)) == 1
        # Idempotent: nothing is left to recover.
        assert fix.recover() == ()
    finally:
        application.close_delegation(timeout=10)

    second = build_application(config=_config(tmp_path))
    try:
        assert recovered[-1] == ()
    finally:
        second.close_delegation(timeout=10)

    # The restore still returns the originals from the interrupted fix. A
    # later runtime owns a newer epoch (the host epoch is ``time_ns()``).
    journal = journal_at(tmp_path, verifier=True, db=tmp_path / "state" / "worker-effects.db")
    worker = "build-fix:" + _config(tmp_path).compute.node_id
    restarted = fix_service(tmp_path, journal, time.time_ns(), worker=worker)
    restored = restarted.service.restore(job, ctx())
    assert restored["restored"] == [REL]
    assert (tmp_path / "proj" / REL).read_text() == ORIGINAL


def test_a_failing_build_fix_recovery_never_blocks_startup(caplog):
    from types import SimpleNamespace

    from sonder_runtime.bootstrap.app import _recover_interrupted_build_fixes

    class Broken:
        def recover(self):
            raise OSError("/private/state/path unreadable")

    with caplog.at_level("WARNING", logger="sonder_runtime.bootstrap.app"):
        assert _recover_interrupted_build_fixes(SimpleNamespace(fix=Broken())) == ()
    assert "OSError" in caplog.text and "/private/state/path" not in caplog.text
    assert _recover_interrupted_build_fixes(None) == ()
    assert _recover_interrupted_build_fixes(SimpleNamespace(fix=None)) == ()


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] in ALL_CUTS:
    _child(sys.argv[1], Path(sys.argv[2]), sys.argv[3] == "composed")
