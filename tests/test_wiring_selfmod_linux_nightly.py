"""End-to-end wiring: nightly selfmod on Linux under the uid-separated supervisor.

These tests drive the real entry point, ``scripts.nightly_selfmod.run``,
against a real Git checkout, the real ``selfmod`` ledger, the real Linux
candidate supervisor and the bootstrap-composed selfmod stage journal.  Only
the local model is replaced (``propose_objective`` and ``_ask``: the model's
proposal and its rewritten function), because no model is available here.

The supervisor must switch uids, so the dry cycles need Linux and euid 0.
The refusal test runs on any Linux host.
"""

from __future__ import annotations

import hashlib
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

LINUX = sys.platform.startswith("linux")
ROOT = LINUX and hasattr(os, "geteuid") and os.geteuid() == 0
needs_root = pytest.mark.skipif(
    not ROOT, reason="Linux uid-separated selfmod supervisor needs Linux and euid 0",
)
# A dedicated, otherwise unused uid for this test process (RLIMIT_NPROC is per
# real uid, so it must not be shared with another xdist worker).
CANDIDATE_UID = 260_000 + os.getpid() % 30_000

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
HELD_OUT = (
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


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=60,
    )
    return completed.stdout.strip()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def host(monkeypatch):
    """A root-owned 0755 host: stable checkout, selfmod state and runtime home."""
    import selfmod

    area = Path(tempfile.mkdtemp(prefix="sonder-wiring-selfmod-", dir="/tmp"))
    area.chmod(0o755)
    repo = area / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "reflection.py").write_text(ORIGINAL, encoding="utf-8")
    (repo / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    heavy_memory: heavy\n"
        "    requires_medium_integrity: medium\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_reflection.py").write_text(HELD_OUT, encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text(
        "from reflection import selected, unrelated\n\n\n"
        "def test_other():\n    assert unrelated([selected(1)]) == 1\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_heavy.py").write_text(
        "import pytest\n\nfrom reflection import unrelated\n\n\n"
        "@pytest.mark.heavy_memory\ndef test_heavy():\n    assert unrelated(range(3)) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "wiring@example.invalid")
    _git(repo, "config", "user.name", "wiring")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for path in [repo, *repo.rglob("*")]:
        if ".git" not in path.parts:
            path.chmod(0o755 if path.is_dir() else 0o644)

    home = area / "home"
    home.mkdir(mode=0o755)
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(area / "selfmod"))
    monkeypatch.delenv("SONDER_SELFMOD_DB", raising=False)
    monkeypatch.setenv("SONDER_SELFMOD_REGRESSION_WORKERS", "1")
    monkeypatch.setattr(nightly_selfmod, "REPO", repo)
    monkeypatch.setattr(nightly_selfmod, "_test_python", lambda: sys.executable)
    # Ruff is an optional host tool; its cache writes are not under test here.
    monkeypatch.setattr(nightly_selfmod, "_ruff_command", lambda _py: None)
    # The model boundary: the proposal and the rewritten function.
    monkeypatch.setattr(
        nightly_selfmod, "propose_objective",
        lambda *_a, **_k: ("reflection.py", "Guard selected against None input.", "selected"),
    )
    selfmod.set_enabled(True)
    selfmod.set_mode("propose")
    try:
        yield {"area": area, "repo": repo, "home": home}
    finally:
        linux._kill_uid(CANDIDATE_UID) if ROOT else None
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree ") and Path(line[9:]) != repo:
                subprocess.run(["git", "worktree", "remove", "--force", line[9:]],
                               cwd=repo, capture_output=True, check=False)
        shutil.rmtree(area, ignore_errors=True)


def _stages(home: Path):
    """The bootstrap-composed selfmod stage journal (real composition root)."""
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = build_application(config=SonderConfig(state=StateConfig(home=str(home))))
    return application.selfmod_service()


def _rows(run_id: str) -> list[sqlite3.Row]:
    import selfmod

    with sqlite3.connect(selfmod.database_path()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT kind, passed, isolation, exit_code, output FROM selfmod_tests "
            "WHERE run_id=? ORDER BY id", (run_id,),
        ).fetchall()


def _journal_operations(stages, run_id: str) -> list[tuple[str, str]]:
    binding = stages._effect_binding_factory(run_id)
    page = binding.journal.effects_since(binding.run_id, 0, limit=1000)
    return [(record.operation_id, record.state.value) for record in page.records]


def _only_run():
    import selfmod

    runs = selfmod.list_runs(10)
    assert len(runs) == 1, runs
    return runs[0]


@pytest.mark.skipif(not LINUX, reason="Linux supervisor selection contract")
def test_linux_nightly_without_candidate_uid_refuses_with_actionable_message(host, monkeypatch):
    import selfmod

    monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    monkeypatch.setattr(
        nightly_selfmod, "_ask", lambda *_a, **_k: pytest.fail("model asked without isolation"),
    )

    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=60,
                                 stages=_stages(host["home"]))

    assert result.startswith("candidate isolation unavailable, no run started")
    assert linux.CANDIDATE_UID_ENV in result
    assert "REMAINING-SELFMOD-517-LINUX-ISOLATION.md" in result
    assert selfmod.list_runs(10) == []
    assert _git(host["repo"], "status", "--porcelain") == ""


@needs_root
def test_linux_nightly_dry_cycle_runs_candidate_under_uid_supervisor(host, monkeypatch):
    import selfmod

    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    monkeypatch.setattr(nightly_selfmod, "_ask", lambda *_a, **_k: GUARDED_REPLY)
    repo = host["repo"]
    head = _git(repo, "rev-parse", "HEAD")
    live = _digest(repo / "reflection.py")
    stages = _stages(host["home"])
    logs: list[str] = []
    replay_truth: list[tuple[str, ...]] = []
    real_replay = nightly_selfmod.selfmod_host_grader.clean_replay

    def recording_replay(*args, **kwargs):
        replay_truth.append(tuple(str(path) for path in args[9]))
        return real_replay(*args, **kwargs)

    monkeypatch.setattr(nightly_selfmod.selfmod_host_grader, "clean_replay", recording_replay)

    result = nightly_selfmod.run(object(), logs.append, test_timeout=120, stages=stages)

    run = _only_run()
    assert result.startswith("COMMITTED "), (
        result, logs, [(row["kind"], row["output"][-2000:]) for row in _rows(run["id"])])
    rows = _rows(run["id"])
    by_kind = {row["kind"]: row for row in rows}
    assert set(by_kind) >= {"syntax", "regression", "regression_heavy", "held_out",
                            "host_probe", "host_grade"}, [r["kind"] for r in rows]
    for row in rows:
        # Every candidate check was recorded with the Linux supervisor's
        # attestation, and it passed under it.
        assert row["isolation"] == "linux-uid", dict(row)
        assert row["passed"] == 1, dict(row)
    probe_output = by_kind["held_out"]["output"]
    assert '"integrity": "linux-uid"' in probe_output
    assert f'"uid": {CANDIDATE_UID}' in probe_output
    assert '"supervisor_uid": 0' in probe_output
    assert "SELFMOD HELD-OUT CANARY PASSED" in probe_output
    # The parent-scored gate accepted the linux-uid attestation.
    assert "clean checkout: " in by_kind["host_grade"]["output"]
    # The clean replay runs the same candidate bytes under the same
    # evaluator truth as the gates, including the sealed backup bundle.
    assert len(replay_truth) == 1
    assert str(selfmod._backup_dir(run["id"])) in replay_truth[0]

    # Every mutating stage went through the bootstrap-composed journal, with
    # per-attempt identities for the repeatable stages.
    operations = dict(_journal_operations(stages, run["id"]))
    rid = run["id"]
    assert operations[f"selfmod-backup:{rid}"] == "completed"
    assert operations[f"selfmod-prepare-workspace:{rid}"] == "completed"
    assert operations[f"selfmod-begin-testing:{rid}:attempt-1"] == "completed"
    assert operations[f"selfmod-review:{rid}"] == "completed"
    gate_attempts = sorted(op for op in operations if op.startswith(f"selfmod-record-test:{rid}:"))
    assert gate_attempts == sorted(
        f"selfmod-record-test:{rid}:attempt-{n}" for n in range(1, len(gate_attempts) + 1)
    )
    # syntax, regression, regression_heavy, held_out and the host probe.
    assert len(gate_attempts) == 5
    assert all(operations[op] == "completed" for op in gate_attempts)

    # The stable checkout was never written: same HEAD, same bytes, clean.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _digest(repo / "reflection.py") == live
    assert _git(repo, "status", "--porcelain") == ""
    assert f"selfmod/{rid}" in _git(repo, "branch", "--list", f"selfmod/{rid}")
    assert linux.live_uid_pids(CANDIDATE_UID) == set()
    assert selfmod.get_run(rid)["phase"] == "reviewing"


@needs_root
def test_linux_nightly_rejects_tampering_candidate_and_keeps_stable_checkout(host, monkeypatch):
    import selfmod

    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    repo = host["repo"]
    database = selfmod.database_path()
    targets = [str(repo / "reflection.py"), str(repo / "tests" / "test_reflection.py"),
               str(database)]
    # The candidate function tries to overwrite the live checkout, the base
    # held-out suite and the selfmod ledger whenever it is called.
    tamper_reply = (
        "def selected(value):\n"
        '    """Return the selected value unchanged."""\n'
        f"    for target in {targets!r}:\n"
        "        with open(target, 'w') as handle:\n"
        "            handle.write('tampered')\n"
        "    return value\n"
    )
    monkeypatch.setattr(nightly_selfmod, "_ask", lambda *_a, **_k: tamper_reply)
    head = _git(repo, "rev-parse", "HEAD")
    before = {path: _digest(Path(path)) for path in targets[:2]}
    stages = _stages(host["home"])

    result = nightly_selfmod.run(object(), lambda _m: None, test_timeout=120, stages=stages)

    assert result.startswith("candidate rejected: regression failed"), result
    run = _only_run()
    assert run["phase"] in {"rejected", "restored"}
    rows = _rows(run["id"])
    regression = [row for row in rows if row["kind"] == "regression"]
    assert regression and regression[0]["passed"] == 0
    assert regression[0]["isolation"] == "linux-uid"
    assert "Permission denied" in regression[0]["output"]
    assert not any(row["kind"] in {"held_out", "host_grade"} for row in rows)
    # The failed gate is a settled failed effect, not an uncertain one.
    operations = dict(_journal_operations(stages, run["id"]))
    failed = [op for op, state in operations.items()
              if op.startswith("selfmod-record-test:") and state == "failed"]
    assert len(failed) == 1

    # The stable checkout stays healthy: untouched bytes, same HEAD, clean
    # tree, and it still imports and behaves as before.
    assert {path: _digest(Path(path)) for path in targets[:2]} == before
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""
    healthy = subprocess.run(
        [sys.executable, "-c", "import reflection; assert reflection.selected(2) == 2"],
        cwd=repo, capture_output=True, text=True, timeout=60, check=False,
    )
    assert healthy.returncode == 0, healthy.stderr
    assert selfmod.settings()["enabled"] is True  # ledger still readable
    assert linux.live_uid_pids(CANDIDATE_UID) == set()


def test_production_stage_journal_is_the_bootstrap_selfmod_service(tmp_path, monkeypatch):
    """``run()`` without ``stages`` composes the default application's service."""
    from sonder_runtime.application.selfmod.selfmod_service import GuardedLegacySelfmodService

    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    monkeypatch.setenv("SONDER_WORKER_EFFECTS_DB", str(tmp_path / "worker-effects.db"))

    stages = nightly_selfmod._compose_stage_journal()
    assert isinstance(stages, GuardedLegacySelfmodService)
    binding = stages._effect_binding_factory("wiring-probe")
    assert binding.scope == "selfmod-mutation"
    assert binding.run_id == "selfmod:wiring-probe"
