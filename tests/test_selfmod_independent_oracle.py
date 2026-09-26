"""Independent oracle for unattended selfmod candidates (#517, result-frame forgery).

The candidate process writes the stdout the parent grades, and the public
held-out suites are readable by it.  These tests pin the evaluator-held
oracle that closes that gap:

* pure contract (any host): only raw outputs for this run's nonce and tokens
  that equal the held outcomes pass; forged frames, forged pytest summaries,
  replayed frames and rewritten-nonce replays fail; receipts are canonical,
  sealed and bound to candidate and baseline digests;
* ledger wiring (any host): ``record_oracle_grade`` grades in the parent,
  refuses unbound probes, and a non-``linux-uid`` receipt never makes a run
  eligible for unattended promotion;
* adversarial canaries under the real Linux uid supervisor (Linux, euid 0):
  the candidate uid is refused the expected-values file by the kernel, a
  forged frame, a forged pytest summary and replayed outputs from a previous
  nonce are rejected through the real nightly entry point, and a clean
  candidate still advances with an independent receipt; ``review`` only
  auto-approves when that receipt and every existing gate pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts import nightly_selfmod
from scripts import selfmod_linux_isolation as linux
from scripts import selfmod_oracle
from sonder_runtime.application.selfmod.independent_oracle import (
    ORACLE_FRAME_PREFIX,
    CaseSet,
    HeldCase,
    OracleChallenge,
    OracleError,
    OracleReceipt,
    canonical_json,
    grade_frame,
    new_challenge,
)

LINUX = sys.platform.startswith("linux")
ROOT = LINUX and hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = pytest.mark.skipif(
    not ROOT, reason="Linux uid-separated selfmod supervisor needs Linux and euid 0",
)
POSIX = os.name != "nt"
# Dedicated per-worker uid; ranges differ from the other supervisor suites.
CANDIDATE_UID = 300_000 + os.getpid() % 30_000

HELD_CASES = [
    {"args": [None], "expected": 0},
    {"args": [7], "expected": 7},
    {"args": [[1, 2]], "expected": [1, 2]},
    {"args": [], "raises": "TypeError"},
]

ORIGINAL = (
    '"""Selection helpers used by the reflection stage."""\n'
    "\n"
    "\n"
    "def selected(value):\n"
    '    """Return the selected value unchanged."""\n'
    "    return value\n"
    "\n"
    "\n"
    "def unrelated(items):\n"
    '    """Count items; present so a one-function edit is a small diff."""\n'
    "    return len(list(items))\n"
)
GUARDED_REPLY = (
    "def selected(value):\n"
    '    """Return the selected value unchanged."""\n'
    "    if value is None:\n"
    "        return 0\n"
    "    return value\n"
)
PUBLIC_HELD_OUT = (
    "from reflection import selected\n"
    "\n"
    "\n"
    "def test_selected_identity():\n"
    "    assert selected(3) == 3\n"
    "\n"
    "\n"
    "def test_selected_string():\n"
    "    assert selected('x') == 'x'\n"
)


def _case_set() -> CaseSet:
    return CaseSet("reflection", "selected", tuple(HeldCase.from_mapping(c) for c in HELD_CASES))


def _correct_frame(challenge: OracleChallenge, case_set: CaseSet) -> str:
    outputs = {}
    for token, index in zip(challenge.tokens, challenge.order):
        outputs[token] = dict(case_set.cases[index].outcome)
    return ORACLE_FRAME_PREFIX + json.dumps({"nonce": challenge.nonce, "outputs": outputs})


def _oracle_branch(body: str) -> str:
    """A candidate ``selected`` that behaves as identity except in the oracle probe."""
    lines = [
        "def selected(value):",
        '    """Return the selected value unchanged."""',
        "    import json",
        "    import os",
        "    import sys",
        "    try:",
        "        payload = json.loads(sys.argv[1])",
        "    except (IndexError, ValueError):",
        "        payload = {}",
        "    cases = payload.get('cases') if isinstance(payload, dict) else None",
        "    if cases and isinstance(cases[0], dict) and 'token' in cases[0]:",
    ]
    lines += ["        " + line for line in body.splitlines()]
    lines += [
        "        sys.stdout.flush()",
        "        os._exit(0)",
        "    return value",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- pure contract


def test_only_correct_raw_outputs_for_this_challenge_pass():
    case_set = _case_set()
    challenge = new_challenge(len(case_set.cases))

    verdict = grade_frame("noise\n" + _correct_frame(challenge, case_set) + "\ntail",
                          challenge, case_set)

    assert verdict.passed and verdict.matched == 4 and verdict.outputs_sha256
    # The detail never repeats an expected value.
    assert verdict.detail == "candidate outputs matched 4 evaluator-held case(s)"
    # Order-independent: the digest is keyed by held case, not presentation.
    again = new_challenge(len(case_set.cases))
    assert grade_frame(_correct_frame(again, case_set), again,
                       case_set).outputs_sha256 == verdict.outputs_sha256


def test_forged_frame_with_guessed_values_fails():
    case_set = _case_set()
    challenge = new_challenge(len(case_set.cases))
    # A forger that echoes inputs (the public tests' identity behaviour).
    inputs = {item["token"]: {"value": item["args"][0] if item["args"] else None}
              for item in challenge.inputs(case_set)}
    forged = ORACLE_FRAME_PREFIX + json.dumps({"nonce": challenge.nonce, "outputs": inputs})

    verdict = grade_frame(forged, challenge, case_set)

    assert not verdict.passed and verdict.matched == 2
    assert "differ from evaluator-held outcomes" in verdict.detail


def test_forged_pytest_summary_and_host_frame_never_pass():
    case_set = _case_set()
    challenge = new_challenge(len(case_set.cases))
    output = ("============ 4 passed in 0.01s ============\n"
              'SELFMOD HOST CHALLENGE RESULT {"nonce": "%s", "values": [0, 7]}\n'
              "SELFMOD HELD-OUT CANARY PASSED\n" % challenge.nonce)

    verdict = grade_frame(output, challenge, case_set)

    assert not verdict.passed and "exactly one result frame, found 0" in verdict.detail


def test_replayed_frame_from_a_previous_nonce_fails_even_with_nonce_rewritten():
    case_set = _case_set()
    previous = new_challenge(len(case_set.cases))
    recorded = _correct_frame(previous, case_set)
    current = new_challenge(len(case_set.cases))
    assert set(previous.tokens).isdisjoint(current.tokens)

    verbatim = grade_frame(recorded, current, case_set)
    frame = json.loads(recorded[len(ORACLE_FRAME_PREFIX):])
    frame["nonce"] = current.nonce
    rewritten = grade_frame(ORACLE_FRAME_PREFIX + json.dumps(frame), current, case_set)

    assert not verbatim.passed and "another challenge nonce" in verbatim.detail
    assert not rewritten.passed and "exactly this challenge's cases" in rewritten.detail


@pytest.mark.parametrize("mutate, reason", [
    (lambda text: text + "\n" + text, "found 2"),
    (lambda text: text.replace(ORACLE_FRAME_PREFIX, ORACLE_FRAME_PREFIX + "{"), "not JSON"),
    (lambda text: text[:len(ORACLE_FRAME_PREFIX)] + json.dumps(
        {**json.loads(text[len(ORACLE_FRAME_PREFIX):]), "passed": True}), "shape"),
    (lambda text: ORACLE_FRAME_PREFIX + "x" * 70_000, "exceeds its bound"),
])
def test_malformed_frames_fail_closed(mutate, reason):
    case_set = _case_set()
    challenge = new_challenge(len(case_set.cases))

    verdict = grade_frame(mutate(_correct_frame(challenge, case_set)), challenge, case_set)

    assert not verdict.passed and reason in verdict.detail


def test_non_finite_and_unencodable_outputs_do_not_match():
    case_set = CaseSet("m", "f", (HeldCase.from_mapping({"args": [1], "expected": 1.5}),))
    challenge = new_challenge(1)
    token = challenge.tokens[0]
    for observed in ('{"value": NaN}', '{"unencodable": "object"}', '{"value": 1.5, "raised": "X"}'):
        frame = '%s{"nonce": "%s", "outputs": {"%s": %s}}' % (
            ORACLE_FRAME_PREFIX, challenge.nonce, token, observed)
        assert not grade_frame(frame, challenge, case_set).passed


@pytest.mark.parametrize("raw", [
    {"args": [1]},
    {"args": [1], "expected": 1, "raises": "ValueError"},
    {"args": [1], "raises": "not an identifier"},
    {"args": 1, "expected": 1},
    {"args": [float("nan")], "expected": 1},
    {"args": [1], "expected": 1, "extra": True},
])
def test_held_case_contract_is_strict(raw):
    with pytest.raises(OracleError):
        HeldCase.from_mapping(raw)


def test_case_set_rejects_repeated_inputs_and_bad_targets():
    case = HeldCase.from_mapping({"args": [1], "expected": 1})
    with pytest.raises(OracleError):
        CaseSet("m", "f", (case, case))
    with pytest.raises(OracleError):
        CaseSet("m-x", "f", (case,))
    with pytest.raises(OracleError):
        CaseSet.parse({"version": 2, "module": "m", "function": "f", "cases": []})


def _receipt(**overrides) -> OracleReceipt:
    values = dict(
        run_id="run", probe_id=3, attestation="linux-uid", candidate_uid=300_001,
        supervisor_uid=0, case_set_sha256="a" * 64, case_count=4, matched=4,
        nonce="n" * 32, outputs_sha256="b" * 64, confidential=True, read_denied=True,
        passed=True, candidate={"files": {"reflection.py": "c" * 64}, "diff_sha256": "d" * 64},
        baseline={"starting_commit": "e" * 40, "manifest_sha256": "f" * 64},
    )
    values.update(overrides)
    return OracleReceipt(**values)


def test_receipt_is_canonical_sealed_and_bound():
    receipt = _receipt()
    assert receipt.independent
    assert OracleReceipt.from_json(receipt.to_json()) == receipt
    assert receipt.admission_refusal(candidate=receipt.candidate, baseline=receipt.baseline) is None
    other = {"files": {"reflection.py": "0" * 64}, "diff_sha256": "d" * 64}
    assert "different candidate bytes" in receipt.admission_refusal(
        candidate=other, baseline=receipt.baseline)
    assert "different baseline" in receipt.admission_refusal(
        candidate=receipt.candidate,
        baseline={"starting_commit": "0" * 40, "manifest_sha256": "f" * 64})
    tampered = json.loads(receipt.to_json())
    tampered["matched"] = 3
    with pytest.raises(OracleError):
        OracleReceipt.from_json(json.dumps(tampered))


@pytest.mark.parametrize("overrides, reason", [
    ({"attestation": "low", "read_denied": False, "confidential": False}, "does not bound reads"),
    ({"read_denied": False}, "proven unable to read"),
    ({"matched": 3, "passed": False}, "oracle failed"),
])
def test_only_a_proven_linux_uid_pass_is_independent(overrides, reason):
    receipt = _receipt(**overrides)
    assert not receipt.independent
    assert reason in receipt.admission_refusal(candidate=receipt.candidate,
                                               baseline=receipt.baseline)


def test_receipt_cannot_claim_more_than_it_proves():
    with pytest.raises(OracleError):
        _receipt(matched=3)  # passed with a mismatch
    with pytest.raises(OracleError):
        _receipt(confidential=False)  # read denial without confidentiality
    with pytest.raises(OracleError):
        _receipt(candidate={"files": {}, "diff_sha256": "d" * 64})
    with pytest.raises(OracleError):
        _receipt(attestation="unverified")


# ------------------------------------------------------------ store (POSIX)


@pytest.mark.skipif(not POSIX, reason="POSIX modes")
def test_provisioned_case_set_is_evaluator_only(tmp_path, monkeypatch):
    home = tmp_path / "oracle"
    monkeypatch.setenv(selfmod_oracle.ORACLE_HOME_ENV, str(home))

    digest = selfmod_oracle.provision("reflection", "selected", HELD_CASES)
    loaded = selfmod_oracle.load_case_set("reflection", "selected")

    assert loaded.sha256 == digest == selfmod_oracle.current_digest(loaded.path)
    assert loaded.case_set == _case_set()
    assert (home.stat().st_mode & 0o777) == 0o700
    assert (loaded.path.stat().st_mode & 0o777) == 0o600
    assert selfmod_oracle.load_case_set("reflection", "unrelated") is None
    assert selfmod_oracle.main(["inspect", "--module", "reflection", "--function", "selected"]) == 0


@pytest.mark.skipif(not POSIX, reason="POSIX symlinks")
def test_unsafe_or_malformed_case_sets_are_unavailable(tmp_path, monkeypatch):
    home = tmp_path / "oracle"
    home.mkdir(mode=0o700)
    monkeypatch.setenv(selfmod_oracle.ORACLE_HOME_ENV, str(home))
    target = selfmod_oracle.case_path("reflection", "selected")
    decoy = tmp_path / "decoy.json"
    decoy.write_text(_case_set().to_json(), encoding="utf-8")
    target.symlink_to(decoy)
    with pytest.raises(selfmod_oracle.OracleUnavailable):
        selfmod_oracle.load_case_set("reflection", "selected")
    target.unlink()
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(selfmod_oracle.OracleUnavailable):
        selfmod_oracle.load_case_set("reflection", "selected")
    target.write_text(CaseSet("other", "selected", _case_set().cases).to_json(), encoding="utf-8")
    with pytest.raises(selfmod_oracle.OracleUnavailable, match="different target"):
        selfmod_oracle.load_case_set("reflection", "selected")


def test_candidate_cannot_record_an_oracle_grade(tmp_path, monkeypatch):
    import selfmod

    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(tmp_path / "state" / "selfmod.db"))
    repo = _plain_repo(tmp_path / "repo")
    run = selfmod.create_plan(
        "Guard selected against None input.", repo, problem="p", evidence=["e"],
        files=["reflection.py"], criteria=["c"], risk="low",
    )
    selfmod.create_backup(run["id"])
    selfmod.prepare_workspace(run["id"])
    selfmod.apply_candidate_changes(run["id"], {"reflection.py": _guarded_module()})
    selfmod.begin_testing(run["id"])
    with pytest.raises(PermissionError, match="oracle grade"):
        selfmod.record_test(run["id"], "oracle_grade", [sys.executable, "-c", "pass"])


def test_protected_paths_cover_the_oracle():
    import selfmod

    for path in ("scripts/selfmod_oracle.py", "scripts/selfmod_host_grader.py",
                 "sonder_runtime/application/selfmod/independent_oracle.py",
                 "tests/test_selfmod_independent_oracle.py"):
        assert selfmod.is_protected_path(path), path


# ------------------------------------------- ledger wiring (unprivileged seam)


def _plain_repo(root: Path) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "reflection.py").write_text(ORIGINAL, encoding="utf-8")
    (root / "tests" / "test_reflection.py").write_text(PUBLIC_HELD_OUT, encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "oracle@test.invalid"],
                 ["config", "user.name", "oracle"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=60)
    return root


def _guarded_module() -> str:
    return nightly_selfmod._splice_function(ORIGINAL, GUARDED_REPLY, expected_name="selected")


def _module_with(reply: str) -> str:
    spliced = nightly_selfmod._splice_function(ORIGINAL, reply, expected_name="selected")
    assert spliced is not None
    return spliced


@pytest.fixture
def low_seam(tmp_path, monkeypatch):
    """A testing-phase run whose candidate checks go through a low-report seam.

    The seam really executes the candidate command (so the frame is the
    candidate's own stdout) and returns a ``low`` supervisor report, as the
    Windows supervisor would.  The low boundary does not bound reads, so no
    receipt from it may be independent.
    """
    import selfmod
    from scripts import selfmod_low_integrity

    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(tmp_path / "state" / "selfmod.db"))
    monkeypatch.setenv(selfmod_oracle.ORACLE_HOME_ENV, str(tmp_path / "oracle"))
    monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: sys.executable)

    def low(command, *, cwd, timeout, protected_paths=(), **_limits):
        done = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return {"exit_code": done.returncode, "output": done.stdout + done.stderr,
                "passed": done.returncode == 0, "job": {"integrity": "low"}}

    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", low)
    repo = _plain_repo(tmp_path / "repo")
    selfmod_oracle.provision("reflection", "selected", HELD_CASES)

    def start(module_text: str, mode: str = "propose"):
        selfmod.set_mode(mode)
        run = selfmod.create_plan(
            "Guard selected against None input.", repo, problem="p", evidence=["e"],
            files=["reflection.py"], criteria=["c"], risk="low",
        )
        selfmod.create_backup(run["id"])
        selfmod.prepare_workspace(run["id"])
        selfmod.apply_candidate_changes(run["id"], {"reflection.py": module_text})
        selfmod.begin_testing(run["id"])
        return run["id"], selfmod.candidate_path(run["id"])

    return start


def test_low_supervisor_oracle_pass_is_recorded_but_never_independent(low_seam):
    import selfmod

    run_id, workspace = low_seam(_guarded_module(), mode="auto-low-risk")
    loaded = selfmod_oracle.load_case_set("reflection", "selected")

    outcome = nightly_selfmod._independent_oracle_gate(run_id, workspace, loaded, 60)

    assert outcome["passed"] is True and outcome["independent"] is False
    assert "does not bound candidate reads" in outcome["detail"]
    receipt = selfmod.oracle_receipt(run_id)
    assert receipt.attestation == "low" and not receipt.independent
    assert receipt.candidate == selfmod.tested_digests(run_id)
    assert receipt.case_set_sha256 == loaded.sha256
    assert selfmod.record_test(run_id, "syntax", [sys.executable, "-m", "py_compile",
                                                  "reflection.py"], low_integrity=True)["passed"]
    reviewed = selfmod.review(run_id, require_kinds={"syntax"})
    assert reviewed["phase"] == "reviewing"
    assert reviewed["auto_evaluation_eligible"] is False
    with pytest.raises(PermissionError):
        selfmod.approve(run_id, "host:auto-low-risk")
    # The seal is re-verified on read: an edited receipt refuses admission.
    with sqlite3.connect(selfmod.database_path()) as conn:
        text = conn.execute("SELECT receipt_json FROM selfmod_oracle_receipts WHERE run_id=?",
                            (run_id,)).fetchone()[0]
        edited = json.loads(text)
        edited["attestation"], edited["confidential"], edited["read_denied"] = "linux-uid", True, True
        edited["candidate_uid"] = 300_001
        conn.execute("UPDATE selfmod_oracle_receipts SET receipt_json=? WHERE run_id=?",
                     (canonical_json(edited), run_id))
    with pytest.raises(OracleError, match="seal"):
        selfmod.oracle_receipt(run_id)
    assert "could not be verified" in selfmod._oracle_admission_refusal(run_id, selfmod.get_run(run_id))


@pytest.mark.parametrize("body, reason", [
    ("forged = {c['token']: {'value': c['args'][0] if c['args'] else None} for c in cases}\n"
     "sys.stdout.write(%r + json.dumps({'nonce': payload['nonce'], 'outputs': forged}) + '\\n')"
     % ORACLE_FRAME_PREFIX, "differ from evaluator-held outcomes"),
    ("sys.stdout.write('==== 4 passed in 0.01s ====\\n')", "found 0"),
])
def test_forging_candidates_fail_the_parent_comparison(low_seam, body, reason):
    import selfmod

    run_id, workspace = low_seam(_module_with(_oracle_branch(body)))
    loaded = selfmod_oracle.load_case_set("reflection", "selected")

    outcome = nightly_selfmod._independent_oracle_gate(run_id, workspace, loaded, 60)

    assert outcome["passed"] is False and reason in outcome["detail"]
    assert selfmod.oracle_receipt(run_id).passed is False
    reviewed = selfmod.review(run_id, require_kinds={"oracle_probe"})
    assert reviewed["phase"] in {"rejected", "restored"}
    assert "independent oracle failed" in reviewed["last_error"]


def test_oracle_grade_refuses_a_probe_issued_for_another_challenge(low_seam):
    import selfmod

    run_id, workspace = low_seam(_guarded_module())
    loaded = selfmod_oracle.load_case_set("reflection", "selected")
    issued = new_challenge(len(loaded.case_set.cases))
    probe = nightly_selfmod._record_candidate_test(
        run_id, "oracle_probe",
        selfmod_oracle.challenge_command(workspace, issued, loaded.case_set), timeout=60)

    with pytest.raises(PermissionError, match="not issued for this challenge"):
        selfmod.record_oracle_grade(
            run_id, probe["test_id"], challenge=new_challenge(len(loaded.case_set.cases)),
            loaded=loaded, attestation=probe["attestation"], confidential=False,
            read_denied=False)
    # A case set changed after it was loaded is not graded against.
    selfmod_oracle.provision("reflection", "selected", HELD_CASES[:2])
    with pytest.raises(RuntimeError, match="changed after it was loaded"):
        selfmod.record_oracle_grade(
            run_id, probe["test_id"], challenge=issued, loaded=loaded,
            attestation=probe["attestation"], confidential=False, read_denied=False)


def test_nightly_without_held_cases_names_the_oracle_as_not_evaluated(tmp_path, monkeypatch):
    monkeypatch.setenv(selfmod_oracle.ORACLE_HOME_ENV, str(tmp_path / "oracle"))
    loaded, note = nightly_selfmod._load_oracle("reflection.py", "selected")
    assert loaded is None and "no evaluator-held cases for reflection.selected" in note


# ------------------------------------ real Linux uid supervisor (Linux, root)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True, timeout=60).stdout.strip()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def host(monkeypatch):
    """Root-owned 0755 host with a stable checkout, selfmod state and a held set."""
    import selfmod

    area = Path(tempfile.mkdtemp(prefix="sonder-oracle-", dir="/tmp"))
    area.chmod(0o755)
    repo = area / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "reflection.py").write_text(ORIGINAL, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    heavy_memory: heavy\n"
        "    requires_medium_integrity: medium\n", encoding="utf-8")
    (repo / "tests" / "test_reflection.py").write_text(PUBLIC_HELD_OUT, encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text(
        "from reflection import selected, unrelated\n\n\n"
        "def test_other():\n    assert unrelated([selected(1)]) == 1\n", encoding="utf-8")
    (repo / "tests" / "test_heavy.py").write_text(
        "import pytest\n\nfrom reflection import unrelated\n\n\n"
        "@pytest.mark.heavy_memory\ndef test_heavy():\n    assert unrelated(range(3)) == 3\n",
        encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "oracle@example.invalid"],
                 ["config", "user.name", "oracle"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        _git(repo, *args)
    for path in [repo, *repo.rglob("*")]:
        if ".git" not in path.parts:
            path.chmod(0o755 if path.is_dir() else 0o644)
    home = area / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(area / "selfmod"))
    monkeypatch.delenv("SONDER_SELFMOD_DB", raising=False)
    # Composing the application graph re-homes selfmod state, so the oracle
    # home is pinned explicitly (root-owned, beside the other host state).
    monkeypatch.setenv(selfmod_oracle.ORACLE_HOME_ENV, str(area / "oracle"))
    monkeypatch.setenv("SONDER_SELFMOD_REGRESSION_WORKERS", "1")
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    monkeypatch.setattr(nightly_selfmod, "REPO", repo)
    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: sys.executable)
    monkeypatch.setattr(nightly_selfmod, "_ruff_command", lambda _py: None)
    monkeypatch.setattr(
        nightly_selfmod, "propose_objective",
        lambda *_a, **_k: ("reflection.py", "Guard selected against None input.", "selected"))
    selfmod.set_enabled(True)
    selfmod.set_mode("propose")
    selfmod_oracle.provision("reflection", "selected", HELD_CASES)
    try:
        yield {"area": area, "repo": repo, "home": home,
               "case_file": selfmod_oracle.case_path("reflection", "selected")}
    finally:
        linux._kill_uid(CANDIDATE_UID)
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree ") and Path(line[9:]) != repo:
                subprocess.run(["git", "worktree", "remove", "--force", line[9:]],
                               cwd=repo, capture_output=True, check=False)
        shutil.rmtree(area, ignore_errors=True)


def _stages(home: Path):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    return build_application(config=SonderConfig(state=StateConfig(home=str(home)))).selfmod_service()


def _rows(run_id: str):
    import selfmod

    with sqlite3.connect(selfmod.database_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT kind, passed, isolation, output FROM selfmod_tests "
                            "WHERE run_id=? ORDER BY id", (run_id,)).fetchall()


def _latest_run():
    import selfmod

    return selfmod.list_runs(1)[0]


def _cycle(host, monkeypatch, reply: str, stages=None):
    monkeypatch.setattr(nightly_selfmod, "_ask", lambda *_a, **_k: reply)
    logs: list[str] = []
    result = nightly_selfmod.run(object(), logs.append, test_timeout=120,
                                 stages=stages or _stages(host["home"]))
    run = _latest_run()
    return result, run, {row["kind"]: row for row in _rows(run["id"])}, logs


@needs_root
def test_candidate_uid_is_refused_the_held_expected_values(host):
    case_file = host["case_file"]
    reader = [sys.executable, "-I", "-c",
              "import sys; print(open(sys.argv[1]).read())", str(case_file)]

    denied = linux.run_isolated(reader, cwd=host["repo"], timeout=30,
                                protected_paths=(case_file,))

    assert denied["exit_code"] != 0 and "PermissionError" in denied["output"]
    assert '"expected"' not in denied["output"]
    assert selfmod_oracle.confidentiality(case_file) == (
        True, "held expected values are closed to the candidate uid")
    assert selfmod_oracle.prove_read_denied(case_file, host["repo"], 30)[0] is True

    # An operator mistake that exposes the set is detected before grading and
    # by the kernel-level probe; such a verdict can never be independent.
    case_file.parent.chmod(0o755)
    case_file.chmod(0o644)
    confidential, why = selfmod_oracle.confidentiality(case_file)
    proved, detail = selfmod_oracle.prove_read_denied(case_file, host["repo"], 30)
    assert confidential is False and "candidate-readable" in why
    assert proved is False and "read=ALLOWED" in detail
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


@needs_root
def test_clean_candidate_advances_with_an_independent_receipt(host, monkeypatch):
    import selfmod

    repo = host["repo"]
    head = _git(repo, "rev-parse", "HEAD")
    live = _digest(repo / "reflection.py")
    stages = _stages(host["home"])

    result, run, by_kind, logs = _cycle(host, monkeypatch, GUARDED_REPLY, stages)

    assert result.startswith("COMMITTED "), (result, logs)
    assert "oracle_probe" in by_kind, logs
    assert by_kind["oracle_probe"]["isolation"] == "linux-uid"
    assert by_kind["oracle_grade"]["passed"] == 1
    assert "independent=yes" in by_kind["oracle_grade"]["output"]
    assert "  oracle_grade: pass (independent=yes)" in logs
    receipt = selfmod.oracle_receipt(run["id"])
    assert receipt.independent and receipt.candidate_uid == CANDIDATE_UID
    assert receipt.supervisor_uid == 0 and receipt.attestation == "linux-uid"
    assert receipt.candidate == selfmod.tested_digests(run["id"])
    assert receipt.baseline["starting_commit"] == head
    assert receipt.case_set_sha256 == _digest(host["case_file"])
    # The oracle gate is the sixth journaled candidate check.
    binding = stages._effect_binding_factory(run["id"])
    records = binding.journal.effects_since(binding.run_id, 0, limit=1000).records
    attempts = [r for r in records if r.operation_id.startswith(f"selfmod-record-test:{run['id']}:")]
    assert len(attempts) == 6 and all(r.state.value == "completed" for r in attempts)
    # Automatic promotion still stops for a human: the medium partition is
    # never evaluated on this path.
    assert "regression_medium NOT EVALUATED" in result
    assert selfmod.get_run(run["id"])["auto_evaluation_eligible"] is False
    assert _git(repo, "rev-parse", "HEAD") == head and _digest(repo / "reflection.py") == live
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


@needs_root
@pytest.mark.parametrize("forger", ["frame", "pytest-summary"])
def test_forging_candidate_is_rejected_by_the_real_nightly(host, monkeypatch, forger):
    import selfmod

    case_file = str(host["case_file"])
    steal = (
        "stolen = None\n"
        "try:\n"
        "    stolen = open(%r).read()\n"
        "except OSError as exc:\n"
        "    sys.stdout.write('oracle read refused: %%s\\n' %% exc)\n" % case_file
    )
    if forger == "frame":
        body = steal + (
            "forged = {c['token']: {'value': c['args'][0] if c['args'] else None} for c in cases}\n"
            "sys.stdout.write(%r + json.dumps({'nonce': payload['nonce'], 'outputs': forged}) + '\\n')"
            % ORACLE_FRAME_PREFIX)
    else:
        body = steal + (
            "sys.stdout.write('==== 4 passed in 0.01s ====\\n')\n"
            "sys.stdout.write('SELFMOD HOST CHALLENGE RESULT ' + json.dumps("
            "{'nonce': payload['nonce'], 'values': [0, 7, [1, 2]]}) + '\\n')")
    repo = host["repo"]
    head = _git(repo, "rev-parse", "HEAD")

    result, run, by_kind, _logs = _cycle(host, monkeypatch, _oracle_branch(body))

    assert result == "candidate rejected: independent oracle failed", result
    # The candidate passed every public gate: only the held oracle caught it.
    for kind in ("regression", "held_out", "host_probe", "host_grade", "oracle_probe"):
        assert by_kind[kind]["passed"] == 1 and by_kind[kind]["isolation"] == "linux-uid", kind
    probe_output = by_kind["oracle_probe"]["output"]
    assert "oracle read refused: [Errno 13] Permission denied" in probe_output
    assert by_kind["oracle_grade"]["passed"] == 0
    receipt = selfmod.oracle_receipt(run["id"])
    assert receipt.passed is False and not receipt.independent
    assert selfmod.get_run(run["id"])["phase"] in {"rejected", "restored"}
    assert _git(repo, "rev-parse", "HEAD") == head
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


@needs_root
def test_replayed_outputs_from_a_previous_nonce_are_rejected(host, monkeypatch):
    clean, _run, by_kind, _logs = _cycle(host, monkeypatch, GUARDED_REPLY)
    assert clean.startswith("COMMITTED ")
    frames = [line for line in by_kind["oracle_probe"]["output"].splitlines()
              if line.startswith(ORACLE_FRAME_PREFIX)]
    assert len(frames) == 1
    recorded = frames[0][len(ORACLE_FRAME_PREFIX):]

    for rewrite, reason in ((False, "another challenge nonce"),
                            (True, "exactly this challenge's cases")):
        body = "frame = json.loads(%r)\n" % recorded
        if rewrite:
            body += "frame['nonce'] = payload['nonce']\n"
        body += "sys.stdout.write(%r + json.dumps(frame) + '\\n')" % ORACLE_FRAME_PREFIX
        result, _run, rows, _logs = _cycle(host, monkeypatch, _oracle_branch(body))
        assert result == "candidate rejected: independent oracle failed", result
        assert reason in rows["oracle_grade"]["output"], rows["oracle_grade"]["output"]
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


@needs_root
def test_host_auto_approval_requires_the_independent_oracle_and_existing_gates(host):
    import selfmod

    repo = host["repo"]
    selfmod.set_mode("auto-low-risk")
    held = {"host_cases": ({"args": [3], "kwargs": {}, "expected": 3},), "protected_paths": ()}

    def gated_run(with_oracle: bool):
        run = selfmod.create_plan(
            "Guard selected against None input.", str(repo), problem="p", evidence=["e"],
            files=["reflection.py"], criteria=["c"], risk="low",
        )
        assert run["approval_required"] is False
        rid = run["id"]
        selfmod.create_backup(rid)
        selfmod.prepare_workspace(rid)
        selfmod.apply_candidate_changes(rid, {"reflection.py": _guarded_module()})
        selfmod.begin_testing(rid)
        workspace = selfmod.candidate_path(rid)
        truth = nightly_selfmod._evaluator_truth_paths(rid, held)
        assert selfmod.record_test(rid, "syntax", [sys.executable, "-m", "py_compile",
                                                   "reflection.py"], protected_paths=truth,
                                   low_integrity=True)["isolation"] == "linux-uid"
        assert nightly_selfmod._parent_scored_gate(
            rid, workspace, "reflection.py", "selected", held, 60,
            protected_paths=truth)["passed"]
        if with_oracle:
            loaded = selfmod_oracle.load_case_set("reflection", "selected")
            outcome = nightly_selfmod._independent_oracle_gate(
                rid, workspace, loaded, 60, protected_paths=truth)
            assert outcome["passed"] and outcome["independent"], outcome
        return selfmod.review(rid, require_kinds={"syntax"})

    without = gated_run(with_oracle=False)
    assert without["phase"] == "reviewing" and without["auto_evaluation_eligible"] is False
    assert "no independent oracle receipt" in selfmod.events(without["id"])[-1]["details"]
    with pytest.raises(PermissionError):
        selfmod.approve(without["id"], "host:auto-low-risk")
    selfmod.cancel(without["id"])

    with_oracle = gated_run(with_oracle=True)
    assert with_oracle["auto_evaluation_eligible"] is True
    assert with_oracle["phase"] == "approved"
    assert with_oracle["approved_by"] == "host:auto-low-risk"
    assert linux.live_uid_pids(CANDIDATE_UID) == set()
