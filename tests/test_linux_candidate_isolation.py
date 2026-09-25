"""OS-enforced Linux uid boundary for unattended selfmod candidates (#517).

These tests run real candidate processes under a distinct unprivileged uid.
The supervisor must be able to switch uids, so they need Linux and euid 0;
ordinary non-root CI skips them with that reason.  A green run here is
root-container/host qualification evidence, not CI qualification.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not hasattr(os, "geteuid")
    or os.geteuid() != 0,
    reason="Linux uid-separated selfmod supervisor needs Linux and euid 0 to switch uids",
)

# A dedicated, otherwise unused uid per test process: RLIMIT_NPROC counts every
# process of a real uid, so sharing ``nobody`` (or another xdist worker's uid)
# would skew the process bound and trip the spare-uid pre-launch check.
CANDIDATE_UID = 200_000 + os.getpid() % 50_000


@pytest.fixture
def area():
    """A root-owned 0755 evaluator area the candidate may read but not write."""
    root = Path(tempfile.mkdtemp(prefix="sonder-517-", dir="/tmp"))
    root.chmod(0o755)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run(command, cwd, **kwargs):
    from scripts.selfmod_linux_isolation import run_isolated

    kwargs.setdefault("timeout", 20)
    return run_isolated(
        command, cwd=cwd, candidate_uid=CANDIDATE_UID,
        candidate_gid=CANDIDATE_UID, **kwargs,
    )


def _python(source: str) -> list[str]:
    return [sys.executable, "-I", "-c", source]


def _truth(path: Path, text: str = "trusted\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _running(pid: int) -> bool:
    """True when ``pid`` exists and is not a zombie."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return line.split()[1] != "Z"
    return False


def _no_candidate_processes() -> bool:
    from scripts.selfmod_linux_isolation import live_uid_pids

    return live_uid_pids(CANDIDATE_UID) == set()


def test_candidate_runs_as_distinct_uid_and_cannot_write_protected_file(area):
    truth = _truth(area / "evaluator" / "truth.txt")
    before = _digest(truth)
    source = (
        "import errno, os, resource\n"
        "from pathlib import Path\n"
        "print('uid=%d gid=%d groups=%r' % (os.getuid(), os.getgid(), os.getgroups()))\n"
        "status = Path('/proc/self/status').read_text()\n"
        "print('nnp=' + [l for l in status.splitlines() if l.startswith('NoNewPrivs')][0].split()[1])\n"
        "print('nproc=%d' % resource.getrlimit(resource.RLIMIT_NPROC)[0])\n"
        "try:\n"
        f"    Path({str(truth)!r}).write_text('tampered')\n"
        "except PermissionError as exc:\n"
        "    print('denied errno=%d' % exc.errno)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )

    result = _run(_python(source), area, protected_paths=[truth], active_processes=16)

    assert result["passed"] is True, result
    assert result["exit_code"] == 0
    job = result["job"]
    assert job["integrity"] == "linux-uid"
    assert job["uid"] == CANDIDATE_UID != os.geteuid()
    assert job["gid"] == CANDIDATE_UID
    assert job["supervisor_uid"] == os.geteuid()
    assert f"uid={CANDIDATE_UID} gid={CANDIDATE_UID} groups=[]" in result["output"]
    assert "nnp=1" in result["output"]
    assert "nproc=16" in result["output"]
    assert "denied errno=13" in result["output"]
    assert _digest(truth) == before
    assert _no_candidate_processes()


def test_candidate_cannot_overwrite_held_out_suite(area):
    suite = _truth(area / "heldout" / "test_hidden.py", "def test_hidden():\n    assert 1\n")
    before = _digest(suite)
    source = (
        "import os\n"
        f"p = {str(suite)!r}\n"
        "denied = 0\n"
        "for attempt in (lambda: open(p, 'w'), lambda: open(p, 'a'),\n"
        "                lambda: os.truncate(p, 0), lambda: os.chmod(p, 0o666),\n"
        "                lambda: os.utime(p, (0, 0))):\n"
        "    try:\n"
        "        attempt()\n"
        "    except PermissionError:\n"
        "        denied += 1\n"
        "print('denied', denied)\n"
        "raise SystemExit(0 if denied == 5 else 3)\n"
    )
    result = _run(_python(source), area, protected_paths=[suite])
    assert result["passed"] is True, result
    assert _digest(suite) == before
    assert oct(suite.stat().st_mode & 0o777) == oct(0o644)


def test_candidate_cannot_replace_protected_file_through_parent_directory(area):
    truth = _truth(area / "evaluator" / "decision.json", '{"verdict": "reject"}\n')
    before = _digest(truth)
    source = (
        "import os\n"
        f"p = {str(truth)!r}\n"
        "forged = os.path.join(os.environ['TMPDIR'], 'forged.json')\n"
        "open(forged, 'w').write('{\"verdict\": \"accept\"}')\n"
        "outcomes = []\n"
        "for attempt in (lambda: os.unlink(p), lambda: os.rename(p, p + '.bak'),\n"
        "                lambda: open(p + '.new', 'w')):\n"
        "    try:\n"
        "        attempt()\n"
        "        outcomes.append('allowed')\n"
        "    except PermissionError:\n"
        "        outcomes.append('denied')\n"
        "try:\n"
        "    os.replace(forged, p)\n"
        "    outcomes.append('allowed')\n"
        "except OSError:\n"
        "    outcomes.append('denied')\n"
        "print(outcomes)\n"
        "raise SystemExit(0 if set(outcomes) == {'denied'} else 3)\n"
    )
    result = _run(_python(source), area, protected_paths=[truth])
    assert result["passed"] is True, result
    assert _digest(truth) == before
    assert sorted(item.name for item in truth.parent.iterdir()) == ["decision.json"]


def test_candidate_cannot_swap_symlink_to_evaluator_config(area):
    config = _truth(area / "evaluator" / "evaluator.cfg", "threshold=0.9\n")
    link = area / "evaluator" / "active.cfg"
    link.symlink_to(config.name)
    before = _digest(config)
    source = (
        "import os\n"
        f"link, config = {str(link)!r}, {str(config)!r}\n"
        "outcomes = []\n"
        "decoy = os.path.join(os.environ['TMPDIR'], 'decoy.cfg')\n"
        "open(decoy, 'w').write('threshold=0.0\\n')\n"
        "staged = os.path.join(os.environ['TMPDIR'], 'staged-link')\n"
        "os.symlink(decoy, staged)\n"
        "for attempt in (lambda: os.replace(staged, link), lambda: os.unlink(link),\n"
        "                lambda: open(link, 'w'), lambda: os.symlink(decoy, link + '.2')):\n"
        "    try:\n"
        "        attempt()\n"
        "        outcomes.append('allowed')\n"
        "    except OSError:\n"
        "        outcomes.append('denied')\n"
        "print(outcomes)\n"
        "raise SystemExit(0 if set(outcomes) == {'denied'} else 3)\n"
    )
    result = _run(_python(source), area, protected_paths=[config])
    assert result["passed"] is True, result
    assert os.readlink(link) == config.name
    assert _digest(config) == before
    assert not (area / "evaluator" / "active.cfg.2").exists()


def test_fork_bomb_is_bounded_by_active_processes(area):
    source = (
        "import os, time\n"
        "children = 0\n"
        "try:\n"
        "    for _ in range(64):\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            time.sleep(30)\n"
        "            os._exit(0)\n"
        "        children += 1\n"
        "except OSError as exc:\n"
        "    print('refused after', children, exc.errno)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    started = time.monotonic()
    result = _run(_python(source), area, active_processes=6, timeout=20)
    assert result["passed"] is True, result
    match = re.search(r"refused after (\d+)", result["output"])
    assert match and int(match.group(1)) < 6
    assert result["job"]["limits"]["active_processes"] == 6
    # Sleeping forked children were torn down with the candidate, not waited on.
    assert result["job"]["lingering_processes_killed"] >= 1
    assert time.monotonic() - started < 15
    assert _no_candidate_processes()


def test_allocation_beyond_process_memory_bound_is_denied(area):
    source = (
        "try:\n"
        "    block = bytearray(1024 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    print('allocation denied')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    result = _run(_python(source), area, process_memory_mb=256)
    assert result["passed"] is True, result
    assert "allocation denied" in result["output"]
    assert result["job"]["limits"]["process_memory_mb"] == 256


def test_job_memory_bound_kills_the_candidate_tree(area):
    source = (
        "import time\n"
        "block = bytearray(b'\\x01') * (160 * 1024 * 1024)\n"
        "time.sleep(15)\n"
    )
    started = time.monotonic()
    result = _run(_python(source), area, process_memory_mb=512, job_memory_mb=64, timeout=20)
    assert result["passed"] is False
    assert result["exit_code"] != 0
    assert result["job"]["limit_hit"] == "job_memory"
    assert result["job"]["peak_job_memory_mb"] > 64
    assert time.monotonic() - started < 15
    assert _no_candidate_processes()


def test_timeout_kills_detached_grandchild(area):
    source = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    if os.fork() == 0:\n"
        "        print('GRANDCHILD %d' % os.getpid(), flush=True)\n"
        "        time.sleep(120)\n"
        "    os._exit(0)\n"
        "time.sleep(120)\n"
    )
    started = time.monotonic()
    result = _run(_python(source), area, timeout=3)
    elapsed = time.monotonic() - started
    assert result["exit_code"] == 124 and result["passed"] is False
    assert result["job"]["timed_out"] is True
    match = re.search(r"GRANDCHILD (\d+)", result["output"])
    assert match, result["output"]
    assert not _running(int(match.group(1)))
    assert elapsed < 15
    assert _no_candidate_processes()


def test_detached_grandchild_does_not_outlive_a_clean_exit(area):
    source = (
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    print('GRANDCHILD %d' % os.getpid(), flush=True)\n"
        "    time.sleep(120)\n"
        "    os._exit(0)\n"
        "time.sleep(0.2)\n"
    )
    started = time.monotonic()
    result = _run(_python(source), area, timeout=20)
    assert result["passed"] is True and result["exit_code"] == 0, result
    assert result["job"]["lingering_processes_killed"] >= 1
    match = re.search(r"GRANDCHILD (\d+)", result["output"])
    assert match and not _running(int(match.group(1)))
    assert time.monotonic() - started < 10
    assert _no_candidate_processes()


def _exposed(area: Path, kind: str) -> Path:
    truth = _truth(area / "evaluator" / "truth.txt")
    if kind == "world-writable-ancestor":
        truth.parent.chmod(0o777)
    elif kind == "candidate-owned-ancestor":
        os.chown(truth.parent, CANDIDATE_UID, 0)
    elif kind == "candidate-owned-file":
        os.chown(truth, CANDIDATE_UID, 0)
    elif kind == "world-writable-file":
        truth.chmod(0o666)
    elif kind == "candidate-group-writable-file":
        os.chown(truth, 0, CANDIDATE_UID)
        truth.chmod(0o664)
    elif kind == "symlinked-protected-path":
        link = truth.parent / "truth-link.txt"
        link.symlink_to(truth.name)
        return link
    elif kind == "writable-entry-inside-protected-directory":
        (truth.parent / "cache").mkdir(mode=0o777)
        (truth.parent / "cache").chmod(0o777)
        return truth.parent
    return truth


@pytest.mark.parametrize("kind", [
    "world-writable-ancestor", "candidate-owned-ancestor", "candidate-owned-file",
    "world-writable-file", "candidate-group-writable-file",
    "symlinked-protected-path", "writable-entry-inside-protected-directory",
])
def test_prelaunch_refuses_candidate_writable_truth(area, kind):
    from scripts.selfmod_linux_isolation import (
        LinuxIsolationUnavailable,
        ProtectedPathExposed,
    )

    protected = _exposed(area, kind)
    marker = area / "candidate-ran"
    command = _python(f"open({str(marker)!r}, 'w').write('ran')")
    with pytest.raises(ProtectedPathExposed) as caught:
        _run(command, area, protected_paths=[protected])
    assert isinstance(caught.value, LinuxIsolationUnavailable)
    assert not marker.exists()
    assert _no_candidate_processes()


def test_forged_attestation_on_stdout_does_not_change_supervisor_report(area):
    source = (
        "print('SELFMOD ISOLATION: {\"integrity\": \"low\"}')\n"
        "print('{\"integrity\": \"linux-uid\", \"uid\": 0, \"exit_code\": 0}')\n"
        "raise SystemExit(5)\n"
    )
    result = _run(_python(source), area)
    assert result["exit_code"] == 5 and result["passed"] is False
    job = result["job"]
    assert job["integrity"] == "linux-uid"
    assert job["uid"] == CANDIDATE_UID
    assert job["exit"] == {"returncode": 5, "signal": None}
    assert job["timed_out"] is False


@pytest.mark.parametrize("code", [0, 3])
def test_clean_candidate_result_matches_exit_code(area, code):
    result = _run(_python(f"raise SystemExit({code})"), area)
    assert result["exit_code"] == code
    assert result["passed"] is (code == 0)
    assert result["job"]["integrity"] == "linux-uid"
    assert "integrity_failed" not in result


def test_candidate_gets_scrubbed_environment_and_private_home(area, monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "must-not-leak")
    source = (
        "import os, stat\n"
        "home, tmp = os.environ['HOME'], os.environ['TMPDIR']\n"
        "assert 'SONDER_API_KEY' not in os.environ, 'secret leaked'\n"
        "for path in (home, tmp):\n"
        "    info = os.stat(path)\n"
        "    assert info.st_uid == os.getuid(), path\n"
        "    assert stat.S_IMODE(info.st_mode) == 0o700, oct(info.st_mode)\n"
        "open(os.path.join(home, 'cache'), 'w').write('ok')\n"
        "open(os.path.join(tmp, 'scratch'), 'w').write('ok')\n"
        "print('private', home, tmp)\n"
    )
    result = _run(_python(source), area)
    assert result["passed"] is True, result
    homes = re.search(r"private (\S+) (\S+)", result["output"])
    assert homes
    # The private homes are supervisor-created and removed after the run.
    assert not Path(homes.group(1)).exists() and not Path(homes.group(2)).exists()


def test_supervisor_redigest_reports_changed_truth(area):
    truth = _truth(area / "evaluator" / "baseline.json", "{}\n")

    def mutate():
        time.sleep(0.5)
        truth.write_text('{"changed": true}\n', encoding="utf-8")

    changer = threading.Thread(target=mutate)
    changer.start()
    try:
        result = _run(_python("import time; time.sleep(1.5)"), area, protected_paths=[truth])
    finally:
        changer.join()
    assert result["integrity_failed"] is True
    assert result["passed"] is False
    assert "protected truth changed" in result["output"]


def test_selfmod_records_linux_uid_attestation_from_real_supervisor(area, monkeypatch):
    import selfmod

    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    truth = _truth(area / "evaluator" / "truth.txt")
    monkeypatch.setenv("SONDER_SELFMOD_CANDIDATE_UID", str(CANDIDATE_UID))
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)
    monkeypatch.setattr(selfmod, "_run", lambda *_args: pytest.fail("medium execution"))

    result = selfmod._record_command(
        {"id": "auto-linux", "mode": "auto-low-risk", "risk": "low",
         "approval_required": False}, "held_out",
        _python("print('SELFMOD ISOLATION: {\"integrity\": \"low\"}')"), area, 10,
        protected_paths=[truth],
    )
    assert result["passed"] is True, result
    assert result["isolation"] == "linux-uid"
    assert records and records[0][-1] == "linux-uid"


def test_concurrent_supervisor_claim_on_same_uid_fails_closed(area):
    """A second run on a uid that another supervisor holds must not launch.

    Two runs sharing a candidate uid could ptrace or signal each other's
    candidates, and each teardown would kill the other's tree.
    """
    from scripts import selfmod_linux_isolation as linux

    claim = linux._claim_identity(CANDIDATE_UID)
    try:
        marker = area / "launched"
        with pytest.raises(linux.LinuxIsolationUnavailable, match="claimed by another"):
            _run(_python(f"open({str(marker)!r}, 'w').close()"), area)
        assert not marker.exists()
    finally:
        os.close(claim)
    info = linux._CLAIM_DIR.lstat()
    assert info.st_uid == 0 and not info.st_mode & 0o022
    result = _run(_python("pass"), area)
    assert result["passed"] is True, result
    assert _no_candidate_processes()
