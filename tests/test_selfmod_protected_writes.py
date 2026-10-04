"""Protected-write scoring and integrity-aware promotion (#517).

A write during an evaluation run to an agent-instruction or memory file
(``AGENTS.md``, ``CLAUDE.md``, ``.claude/``, Sonder memory/lesson stores),
a ``conftest.py`` or a task test is recorded and invalidates the run's score.
Promotion refuses evidence that carries a protected write or a failed cheat
trial and names it; evidence without either is gated exactly as before.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import selfmod
from sonder_runtime.adapters.evaluation_corpus import BoundedEvaluationCorpusScanner
from sonder_runtime.application.evaluation import integrity
from sonder_runtime.application.evaluation.integrity import (
    ProtectedWriteWatch,
    protected_reason,
    result_violations,
    snapshot_protected,
    violation,
    with_violations,
    write_violations,
)
from sonder_runtime.application.evaluation.promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES,
    PromotionGateEvaluator,
    PromotionKind,
    evaluate_promotion_gate,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension,
    EvaluationMode,
    EvaluationResult,
    EvaluationSuite,
    ProposalLifecycle,
    ProposalState,
    ShadowCanaryObservation,
)
from sonder_runtime.application.evaluation.service import EvaluationApplicationService

PROTECTED = [
    "AGENTS.md", "CLAUDE.md", "docs/CLAUDE.md", "pkg/agents.md", "CLAUDE.local.md",
    ".claude/settings.json", ".claude/skills/x/SKILL.md", "memory.db", "state/memory.db-wal",
    "lessons.jsonl", "conftest.py", "tests/conftest.py", "tests/test_calc.py",
    "tests/unit/calc_test.py",
]
ORDINARY = [
    "README.md", "calc.py", "answer.txt", "tests/data.json", "tests/helpers.py",
    "tests/__pycache__/test_calc.cpython-312.pyc", ".git/AGENTS.md", "node_modules/x/CLAUDE.md",
    "test_top_level.py", "memory.py",
]


@pytest.mark.parametrize("rel", PROTECTED)
def test_protected_paths(rel):
    assert protected_reason(rel)


@pytest.mark.parametrize("rel", ORDINARY)
def test_ordinary_paths(rel):
    assert protected_reason(rel) is None


def test_explicit_task_paths_are_protected():
    assert protected_reason("checks/verify.py", ["checks"]) == "task test"
    assert protected_reason("checks/verify.py") is None


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize("rel", PROTECTED)
def test_writing_any_protected_file_during_a_run_is_recorded(tmp_path, rel):
    watch = ProtectedWriteWatch.start(tmp_path)
    _write(tmp_path, rel, "fetched solution")
    writes = watch.finish()
    assert [(item.path, item.change) for item in writes] == [(rel, "created")]


def test_modification_and_deletion_are_recorded_and_ordinary_writes_are_not(tmp_path):
    _write(tmp_path, "AGENTS.md", "rules")
    _write(tmp_path, "tests/test_calc.py", "def test(): pass\n")
    watch = ProtectedWriteWatch.start(tmp_path)
    (tmp_path / "AGENTS.md").write_text("rules + answers")
    (tmp_path / "tests" / "test_calc.py").unlink()
    for rel in ORDINARY:
        _write(tmp_path, rel)
    writes = watch.finish()
    assert [(item.path, item.change, item.reason) for item in writes] == [
        ("AGENTS.md", "modified", "agent instructions"),
        ("tests/test_calc.py", "deleted", "task test"),
    ]
    assert "modified AGENTS.md (agent instructions)" in integrity.summarize_writes(writes)


def test_extra_roots_cover_a_private_sonder_home(tmp_path):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    workspace.mkdir()
    _write(home, "memory.db", "db")
    watch = ProtectedWriteWatch.start(workspace, extra=[home])
    (home / "memory.db").write_text("db + lesson")
    assert [item.change for item in watch.finish()] == ["modified"]


def test_monitor_failure_is_unverifiable_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(integrity, "MAX_WALK_ENTRIES", 2)
    for index in range(5):
        _write(tmp_path, "f%d.txt" % index)
    writes = ProtectedWriteWatch.start(tmp_path).finish()
    assert [(item.path, item.change) for item in writes] == [("<monitor>", "unverifiable")]


def test_snapshot_is_stable_without_writes(tmp_path):
    _write(tmp_path, "CLAUDE.md")
    assert snapshot_protected(tmp_path) == snapshot_protected(tmp_path)
    assert ProtectedWriteWatch.start(tmp_path).finish() == ()


# --- promotion ----------------------------------------------------------------------

SUITE = EvaluationSuite("prompt-quality", "v1", (EvaluationDimension("split", "holdout"),), ("pass_rate",))
SHADOW = ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 20, {"error": 0}, 0)
CANARY = ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 20, {"error": 0}, 0.05)
POLICY = DEFAULT_PROMOTION_GATE_POLICIES[PromotionKind.PROMPT]


def _result(result_id="r1", passed=40, total=40, provenance=("test",)):
    return EvaluationResult(
        result_id, SUITE, "candidate", "baseline", EvaluationMode.OFFLINE, SUITE.dimensions,
        {"pass_rate": passed / total}, passed == total, total,
        replay_equivalent=True, provenance=provenance,
    )


def _gate(results):
    return evaluate_promotion_gate(POLICY, results=results, baseline_pass_rate=1.0, shadow=SHADOW, canary=CANARY)


# Pinned from origin/main (45a3e093) for the same inputs: a clean candidate's
# decision, including its digest, is unchanged by integrity scoring.
CLEAN_PASS_DIGEST = "62691b14c25f56d9453a1c2136d8696c30cbd6dda0831cccd6b0c217096787e1"
CLEAN_FAIL_REASONS = ("gate_failed:confidence_lower_bound", "gate_failed:sample_size")


def test_clean_evidence_decision_is_unchanged():
    decision = _gate([_result()])
    assert decision.passed
    assert decision.digest == CLEAN_PASS_DIGEST
    assert "evaluation_integrity" not in decision.gate_results
    assert set(decision.gate_results) == {
        "baseline_comparison", "canary", "case_regressions", "confidence_lower_bound", "pass_rate",
        "pass_rate_drop", "replay_equivalence", "sample_size", "shadow",
    }
    thin = _gate([_result(passed=3, total=3)])
    assert thin.reason_codes == CLEAN_FAIL_REASONS
    # A clean result passed through with_violations with nothing to add is the same object.
    clean = _result()
    assert with_violations(clean, ()) is clean


def test_failed_cheat_trial_in_evidence_refuses_the_gate_and_names_it():
    dirty = with_violations(_result(), [violation("cheat_trial", "cheat-answer-file: planted_answer_used")])
    decision = _gate([dirty])
    assert not decision.passed
    assert decision.gate_results["evaluation_integrity"] is False
    assert decision.reason_codes == (
        "gate_failed:evaluation_integrity",
        "evaluation_integrity:cheat_trial:cheat-answer-file: planted_answer_used",
    )


def test_protected_write_in_evidence_refuses_the_gate(tmp_path):
    watch = ProtectedWriteWatch.start(tmp_path)
    _write(tmp_path, "AGENTS.md", "solution")
    dirty = with_violations(_result("r2"), write_violations(watch.finish()))
    decision = _gate([_result("r1"), dirty])
    assert not decision.passed
    assert "evaluation_integrity:protected_write:created AGENTS.md (agent instructions)" in decision.reason_codes


def test_malformed_or_forged_markers_fail_closed():
    for item in ("evaluation-integrity:", "evaluation-integrity:violation:", "evaluation-integrity:ok:clean",
                 "evaluation-integrity:violation:unknown:x"):
        assert [v.kind for v in result_violations([_result(provenance=("test", item))])] == ["malformed"]
    assert not _gate([_result(provenance=("test", "evaluation-integrity:clean"))]).passed


def test_marker_overflow_still_carries_a_violation():
    many = [violation("protected_write", "created f%d" % index) for index in range(80)]
    marked = with_violations(_result(), many)
    assert len(marked.provenance) == 64
    assert marked.provenance[-1].endswith("further violation(s) not listed")


def _service():
    return EvaluationApplicationService(
        corpus=BoundedEvaluationCorpusScanner([]),
        lifecycle=ProposalLifecycle(promotion_gate=PromotionGateEvaluator()),
    )


def _through_canary(service, proposal_id, results, kind=PromotionKind.PROMPT):
    service.register_suite(SUITE)
    service.create_proposal(proposal_id, "candidate", "baseline", SUITE, kind=kind)
    service.submit(proposal_id)
    service.begin_evaluation(proposal_id)
    for result in results:
        service.record_result(proposal_id, result)
    service.begin_shadow(proposal_id)
    service.record_observation(proposal_id, SHADOW)
    service.begin_canary(proposal_id)
    service.record_observation(proposal_id, CANARY)


def _evidence(service, proposal_id):
    return service.gated_promotion_evidence(
        proposal_id, baseline_pass_rate=1.0, case_regressions=0,
        holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
    )


def test_lifecycle_refuses_promotion_with_the_reason():
    service = _service()
    _through_canary(service, "clean", [_result()])
    evidence = _evidence(service, "clean")
    assert service.approve("clean", evidence.digest).state is ProposalState.READY_FOR_PROMOTION

    dirty = with_violations(_result("r-dirty"), [violation("protected_write", "created AGENTS.md (agent instructions)")])
    _through_canary(service, "dirty", [dirty])
    evidence = _evidence(service, "dirty")
    assert not evidence.accepted
    with pytest.raises(ValueError, match="evaluation integrity violation: evaluation_integrity:protected_write:created AGENTS.md"):
        service.approve("dirty", evidence.digest)


def test_legacy_ungated_path_also_refuses():
    service = _service()
    dirty = with_violations(_result(), [violation("cheat_trial", "cheat-test-edit: test_modified")])
    _through_canary(service, "legacy", [dirty], kind=None)
    with pytest.warns(DeprecationWarning):
        evidence = service.promotion_evidence(
            "legacy", gate_results={"quality": True}, replay_equivalent=True,
            holdout_passed=True, rollback_reference="baseline", provenance=("ci",),
        )
    assert evidence.accepted  # caller-asserted gates, as before
    with pytest.raises(ValueError, match="cheat_trial:cheat-test-edit"):
        service.approve("legacy", evidence.digest, allow_ungated_legacy=True)


# --- selfmod ledger -----------------------------------------------------------------


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)


@pytest.fixture
def testing_run(monkeypatch, tmp_path):
    if not shutil.which("git"):
        pytest.skip("git is required to build the selfmod workspace")
    state = tmp_path / "state"
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.delenv("SONDER_SELFMOD_ACTIVE", raising=False)
    monkeypatch.delenv("SELFMOD_LOW_INTEGRITY", raising=False)
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "AGENTS.md").write_text("Run the tests.\n")
    (root / ".gitignore").write_text(".claude/\nmemory.db\nCLAUDE.local.md\ncache/\n")
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    _git(root, "init", "--initial-branch=main")
    _git(root, "-c", "user.email=t@test.invalid", "-c", "user.name=T", "add", ".")
    _git(root, "-c", "user.email=t@test.invalid", "-c", "user.name=T", "commit", "-m", "initial")
    run = selfmod.create_plan(
        "fix addition", root, problem="add subtracts", evidence=["test_add fails"],
        files=["calc.py"], criteria=["tests pass"], risk="low", expected_benefit="correct",
        rollback_plan="restore",
    )
    selfmod.create_backup(run["id"])
    selfmod.prepare_workspace(run["id"])
    selfmod.apply_candidate_changes(run["id"], {"calc.py": "def add(a, b):\n    return a + b\n"})
    selfmod.begin_testing(run["id"])
    return run["id"]


PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]


def _append(rel):
    return [sys.executable, "-c", (
        "import pathlib; p = pathlib.Path(%r); p.parent.mkdir(parents=True, exist_ok=True);"
        "p.write_text((p.read_text() if p.exists() else '') + ' fetched solution')" % rel)]


def _assert_invalidated(run_id, outcome, rel):
    assert outcome["exit_code"] == 0 and not outcome["passed"]
    assert "SELFMOD EVALUATION INTEGRITY: targeted wrote protected evaluation files" in outcome["output"]
    rows = selfmod.test_results(run_id)
    assert [(row["kind"], row["passed"]) for row in rows] == [("targeted", False), ("protected_write", False)]
    assert rel in rows[1]["output"]


# Git-ignored protected files never show up in the candidate diff, so before
# write-scoring nothing noticed them; review now rejects the run and names them.
@pytest.mark.parametrize("rel", [".claude/settings.json", "memory.db", "CLAUDE.local.md", "cache/lessons.jsonl"])
def test_ignored_protected_write_rejects_the_run_with_the_reason(testing_run, rel):
    outcome = selfmod.record_test(testing_run, "targeted", _append(rel), low_integrity=False)
    _assert_invalidated(testing_run, outcome, rel)
    run = selfmod.review(testing_run, require_kinds={"targeted"})
    assert run["phase"] in {"rejected", "restored"}
    assert ("evaluation integrity: protected write during evaluation: targeted wrote protected "
            "evaluation files: created %s" % rel) in run["last_error"]


# A visible write is also a scope escape, which review already refuses by raising.
@pytest.mark.parametrize("rel", ["AGENTS.md", "CLAUDE.md", "conftest.py", "tests/test_calc.py"])
def test_visible_protected_write_invalidates_the_check(testing_run, rel):
    outcome = selfmod.record_test(testing_run, "targeted", _append(rel), low_integrity=False)
    _assert_invalidated(testing_run, outcome, rel)
    with pytest.raises(RuntimeError, match="escaped approved scope"):
        selfmod.review(testing_run, require_kinds={"targeted"})


def test_clean_check_records_exactly_what_it_did_before(testing_run):
    outcome = selfmod.record_test(testing_run, "regression", PYTEST, low_integrity=False)
    assert outcome["passed"], outcome["output"]
    assert "SELFMOD EVALUATION INTEGRITY" not in outcome["output"]
    assert [(row["kind"], row["passed"]) for row in selfmod.test_results(testing_run)] == [("regression", True)]
    run = selfmod.review(testing_run, require_kinds={"regression"})
    assert run["phase"] == "reviewing", run["last_error"]
    assert "evaluation integrity" not in (run["last_error"] or "")


def test_reproducer_in_live_checkout_is_not_watched(testing_run):
    run = selfmod.get_run(testing_run)
    live = Path(run["repository_root"])
    script = "import pathlib; pathlib.Path('CLAUDE.md').write_text('x'); raise SystemExit(1)"
    outcome = selfmod.record_reproducer_before(testing_run, [sys.executable, "-c", script])
    assert outcome["passed"]
    assert [row["kind"] for row in selfmod.test_results(testing_run)] == ["reproducer_before"]
    (live / "CLAUDE.md").unlink()
