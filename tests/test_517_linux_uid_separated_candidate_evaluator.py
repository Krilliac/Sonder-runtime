"""Platform dispatch and fail-closed wiring for the Linux uid supervisor (#517).

These tests do not need root: they pin the documented fail-closed error and
how ``selfmod`` accepts only the attestation its selected supervisor builds.
The real OS boundary is exercised by ``tests/test_linux_candidate_isolation.py``.
"""

from __future__ import annotations

import hashlib
import os
import sys
from contextlib import nullcontext

import pytest

AUTO_RUN = {"id": "auto-517", "mode": "auto-low-risk", "risk": "low",
            "approval_required": False}
# The network boundary and no_new_privs the real supervisor confirms from
# /proc before launch; a linux-uid report without them is not an attestation.
LINUX_NETWORK = {"isolation": "netns", "netns_inode": 4026532262,
                 "supervisor_netns_inode": 4026531833, "interfaces": ["lo"],
                 "loopback_up": False}
LINUX_JOB = {"integrity": "linux-uid", "uid": 210_000, "gid": 210_000,
             "network": LINUX_NETWORK, "no_new_privs": True}


def _no_launch(*_args, **_kwargs):
    raise AssertionError("candidate must not launch")


def test_non_linux_invocation_raises_documented_error(monkeypatch, tmp_path):
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.setattr(linux.sys, "platform", "win32")
    monkeypatch.setattr(linux.subprocess, "Popen", _no_launch)
    with pytest.raises(linux.LinuxIsolationUnavailable, match="requires Linux"):
        linux.run_isolated([sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5,
                           candidate_uid=210_000)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor contract")
def test_non_root_invocation_raises_documented_error(monkeypatch, tmp_path):
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(linux.subprocess, "Popen", _no_launch)
    with pytest.raises(linux.LinuxIsolationUnavailable, match="root"):
        linux.run_isolated([sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5,
                           candidate_uid=210_000)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor contract")
@pytest.mark.parametrize("uid,gid,match", [
    (None, None, "no dedicated candidate uid"),
    (0, 210_000, "unprivileged"),
    (210_000, 0, "unprivileged"),
    (-5, 210_000, "unprivileged"),
])
def test_missing_or_privileged_candidate_identity_fails_closed(
    monkeypatch, tmp_path, uid, gid, match,
):
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
    monkeypatch.setattr(linux.subprocess, "Popen", _no_launch)
    with pytest.raises(linux.LinuxIsolationUnavailable, match=match):
        linux.run_isolated([sys.executable, "-c", "pass"], cwd=tmp_path, timeout=5,
                           candidate_uid=uid, candidate_gid=gid)


def test_candidate_identity_is_read_from_the_environment(monkeypatch):
    from scripts import selfmod_linux_isolation as linux

    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "210001")
    monkeypatch.delenv(linux.CANDIDATE_GID_ENV, raising=False)
    assert linux.candidate_identity(None, None) == (210_001, 210_001)
    monkeypatch.setenv(linux.CANDIDATE_GID_ENV, "210002")
    assert linux.candidate_identity(None, None) == (210_001, 210_002)
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "not-a-uid")
    with pytest.raises(linux.LinuxIsolationUnavailable):
        linux.candidate_identity(None, None)


@pytest.mark.parametrize("platform,configured,expected", [
    ("linux", "210000", "linux-uid"),
    ("linux", "", "low"),
    ("win32", "210000", "low"),
])
def test_supervisor_selection_is_platform_bound(monkeypatch, platform, configured, expected):
    from scripts import selfmod_linux_isolation as linux
    from scripts import selfmod_low_integrity as low

    monkeypatch.setattr(linux.sys, "platform", platform)
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, configured)
    runner, integrity = linux.candidate_supervisor()
    assert integrity == expected
    assert runner is (linux.run_isolated if expected == "linux-uid" else low.run_isolated)


def _record(monkeypatch, tmp_path, *, linux_result=None, low_result=None, configured=True):
    import selfmod
    from scripts import selfmod_linux_isolation as linux
    from scripts import selfmod_low_integrity as low

    records = []
    calls = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    def fake(name, outcome):
        def run(*_args, **_kwargs):
            calls.append(name)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return run

    monkeypatch.setattr(linux.sys, "platform", "linux")
    if configured:
        monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "210000")
    else:
        monkeypatch.delenv(linux.CANDIDATE_UID_ENV, raising=False)
    monkeypatch.setattr(linux, "run_isolated", fake("linux", linux_result))
    monkeypatch.setattr(low, "run_isolated", fake("low", low_result))
    monkeypatch.setattr(selfmod, "_run", lambda *_args: calls.append("medium") or (0, "", 1))
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)
    result = selfmod._record_command(
        AUTO_RUN, "held_out", [sys.executable, "-c", "pass"], tmp_path, 5,
    )
    return result, records, calls


@pytest.mark.parametrize("error", [
    "LinuxIsolationUnavailable", "ProtectedPathExposed",
])
def test_selfmod_maps_linux_fail_closed_error_to_isolation_failure(monkeypatch, tmp_path, error):
    from scripts import selfmod_linux_isolation as linux

    result, records, calls = _record(
        monkeypatch, tmp_path,
        linux_result=getattr(linux, error)("linux-uid isolation requires root"),
    )
    assert calls == ["linux"]
    assert result["exit_code"] == 125 and result["passed"] is False
    assert result["isolation"] == "unverified"
    assert "isolation unavailable" in result["output"]
    assert records and records[0][3] == 125 and records[0][6] == 0
    assert records[0][-1] == "unverified"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux supervisor contract")
def test_selfmod_maps_real_non_root_refusal_to_code_125(monkeypatch, tmp_path):
    import selfmod
    from scripts import selfmod_linux_isolation as linux

    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "210000")
    monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(linux.subprocess, "Popen", _no_launch)
    monkeypatch.setattr(selfmod, "_run", lambda *_args: pytest.fail("medium execution"))
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)
    result = selfmod._record_command(
        AUTO_RUN, "reproducer_after", [sys.executable, "-c", "raise SystemExit(1)"],
        tmp_path, 5, expect_failure=True,
    )
    assert result["exit_code"] == 125 and result["passed"] is False
    assert "linux-uid isolation unavailable" in result["output"]
    assert records[0][-1] == "unverified"


def test_selfmod_accepts_supervisor_built_linux_uid_attestation(monkeypatch, tmp_path):
    result, records, calls = _record(monkeypatch, tmp_path, linux_result={
        "exit_code": 0, "output": "ok", "passed": True,
        "job": dict(LINUX_JOB),
    })
    assert calls == ["linux"]
    assert result["passed"] is True and result["isolation"] == "linux-uid"
    assert records[0][-1] == "linux-uid"


@pytest.mark.parametrize("job", [
    None, {"integrity": "low"}, {"integrity": "LINUX-UID", "uid": 210_000},
    {"integrity": "linux-uid"}, {"integrity": "linux-uid", "uid": 0},
    # A report without the confirmed network/no_new_privs boundary.
    {**LINUX_JOB, "network": None},
    {**LINUX_JOB, "no_new_privs": False},
    {key: value for key, value in LINUX_JOB.items() if key != "no_new_privs"},
    {**LINUX_JOB, "network": {**LINUX_NETWORK, "isolation": "host"}},
    {**LINUX_JOB, "network": {**LINUX_NETWORK, "netns_inode": 4026531833}},
    {**LINUX_JOB, "network": {**LINUX_NETWORK, "interfaces": ["eth0", "lo"]}},
    {**LINUX_JOB, "network": {**LINUX_NETWORK, "loopback_up": True}},
])
def test_selfmod_rejects_linux_supervisor_report_without_its_attestation(
    monkeypatch, tmp_path, job,
):
    result, records, _calls = _record(monkeypatch, tmp_path, linux_result={
        "exit_code": 0, "output": 'SELFMOD ISOLATION: {"integrity": "linux-uid"}',
        "passed": True, "job": job,
    })
    assert result["exit_code"] == 125 and result["passed"] is False
    assert records[0][-1] == "unverified"


def test_windows_supervisor_cannot_claim_linux_uid(monkeypatch, tmp_path):
    result, records, calls = _record(monkeypatch, tmp_path, configured=False, low_result={
        "exit_code": 0, "output": "ok", "passed": True,
        "job": dict(LINUX_JOB),
    })
    assert calls == ["low"]
    assert result["exit_code"] == 125 and records[0][-1] == "unverified"


def test_linux_integrity_failure_is_not_a_pass(monkeypatch, tmp_path):
    result, records, _calls = _record(monkeypatch, tmp_path, linux_result={
        "exit_code": 2, "output": "protected truth changed", "passed": False,
        "integrity_failed": True, "job": dict(LINUX_JOB),
    })
    assert result["exit_code"] == 125 and result["passed"] is False
    assert "evaluator integrity failed" in result["output"]
    assert records[0][-1] == "unverified"


def test_linux_supervisor_and_its_pins_are_protected_paths():
    import selfmod

    prefixes = selfmod.protected_paths()["prefixes"]
    assert "scripts/selfmod_linux_isolation.py" in prefixes
    assert selfmod.is_protected_path("scripts/selfmod_linux_isolation.py")
    assert selfmod.is_protected_path("tests/test_linux_candidate_isolation.py")
    assert selfmod.is_protected_path(
        "tests/test_517_linux_uid_separated_candidate_evaluator.py"
    )


def test_host_grade_binds_to_a_linux_uid_probe(monkeypatch, tmp_path):
    """A linux-uid probe is an attested probe; the grade still needs a human."""
    import selfmod
    from scripts import selfmod_linux_isolation as linux

    state = tmp_path / "state"
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.delenv("SONDER_SELFMOD_ACTIVE", raising=False)
    monkeypatch.setattr(linux.sys, "platform", "linux")
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, "210000")
    monkeypatch.setattr(linux, "run_isolated", lambda *_args, **_kwargs: {
        "exit_code": 0, "output": "candidate output", "passed": True,
        "job": dict(LINUX_JOB),
    })
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    selfmod.set_mode("auto-low-risk")
    run = selfmod.create_plan(
        "fix addition", root, problem="add subtracts",
        evidence=["tests/test_calc.py::test_add fails"], files=["calc.py"],
        criteria=["test passes"], risk="low", expected_benefit="correct",
        rollback_plan="restore hashes",
    )
    selfmod.create_backup(run["id"])
    selfmod.prepare_workspace(run["id"])
    selfmod.apply_candidate_changes(run["id"], {"calc.py": "def add(a, b):\n    return a + b\n"})
    selfmod.record_reproducer_before(run["id"], [sys.executable, "-c", "raise SystemExit(1)"])
    selfmod.begin_testing(run["id"])
    probe = selfmod.record_test(run["id"], "host_probe", [sys.executable, "-c", "print(5)"],
                                low_integrity=True)
    assert probe["isolation"] == "linux-uid"
    grade = selfmod.record_host_grade(run["id"], probe["test_id"], passed=True,
                                      detail="parent compared output")
    assert grade["passed"] is True
    rows = selfmod.test_results(run["id"])
    assert {row["isolation"] for row in rows if row["kind"] == "host_grade"} == {"linux-uid"}


def test_clean_replay_uses_the_selected_supervisor(monkeypatch, tmp_path):
    from scripts import selfmod_host_grader as grader
    from scripts import selfmod_linux_isolation as linux

    seen = {}

    def fake_supervisor():
        def run(command, **kwargs):
            seen["command"] = command
            return {"exit_code": 0, "passed": True, "output": "",
                    "job": {"integrity": "low"}}
        return run, "linux-uid"

    monkeypatch.setattr(linux, "candidate_supervisor", fake_supervisor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "calc.py").write_text("x = 1\n", encoding="utf-8")
    digest = hashlib.sha256((workspace / "calc.py").read_bytes()).hexdigest()

    def fake_worktree(*args, **kwargs):
        command = args[0]
        if command[:3] == ["git", "worktree", "add"]:
            os.makedirs(command[4], exist_ok=True)
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(grader.subprocess, "run", fake_worktree)
    ok, detail = grader.clean_replay(
        tmp_path / "repo", workspace, tmp_path / "state", "abc123",
        {"calc.py": digest}, "calc", "add",
        ({"args": [1, 2], "kwargs": {}, "expected": 3},), 5,
    )
    # A "low" report from the supervisor selected for linux-uid is refused.
    assert seen["command"]
    assert ok is False and "isolated probe" in detail
