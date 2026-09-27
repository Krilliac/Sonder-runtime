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


# --- network namespace and no_new_privs boundary -----------------------------


def _host_ipv4_addresses() -> list[str]:
    """Every IPv4 address configured on the host (loopback first)."""
    import fcntl
    import socket
    import struct

    found = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        for _index, name in socket.if_nameindex():
            try:
                # SIOCGIFADDR (linux/sockios.h): the interface's IPv4 address.
                reply = fcntl.ioctl(probe.fileno(), 0x8915,
                                    struct.pack("256s", name.encode()[:15]))
            except OSError:
                continue
            found.append(socket.inet_ntoa(reply[20:24]))
    return sorted(set(found), key=lambda address: not address.startswith("127."))


def _host_listeners():
    """Listening TCP sockets on 127.0.0.1, ::1 and every non-loopback address."""
    import socket

    listeners = []
    for address in _host_ipv4_addresses() or ["127.0.0.1"]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind((address, 0))
        listeners.append(server)
    try:
        server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        server = None  # this kernel has no IPv6 at all
    if server is not None:
        try:
            server.bind(("::1", 0))
        except OSError:
            server.close()
        else:
            listeners.append(server)
    for server in listeners:
        server.listen(8)
    return listeners


def test_candidate_runs_in_isolated_network_namespace_and_report_attests_it(area):
    from sonder_runtime.application.selfmod.candidate_isolation import IsolationAttestation

    source = (
        "import os, socket\n"
        "print('netns=%d' % os.stat('/proc/self/ns/net').st_ino)\n"
        "print('ifaces=%r' % sorted(name for _i, name in socket.if_nameindex()))\n"
        "status = open('/proc/self/status').read().splitlines()\n"
        "print('nnp=' + [l for l in status if l.startswith('NoNewPrivs')][0].split()[1])\n"
    )
    result = _run(_python(source), area)

    assert result["passed"] is True, result
    host_netns = os.stat("/proc/self/ns/net").st_ino
    network = result["job"]["network"]
    assert network["isolation"] == "netns"
    assert network["supervisor_netns_inode"] == host_netns
    assert network["netns_inode"] != host_netns
    assert network["interfaces"] == ["lo"] and network["loopback_up"] is False
    assert result["job"]["no_new_privs"] is True
    # The candidate's own view matches what the supervisor attested.
    assert f"netns={network['netns_inode']}" in result["output"]
    assert "ifaces=['lo']" in result["output"]
    assert "nnp=1" in result["output"]
    typed = result["attestation"]
    assert isinstance(typed, IsolationAttestation)
    assert typed.network_isolated is True and typed.no_new_privs is True
    # The supervisor itself stayed in the host namespace.
    assert os.stat("/proc/self/ns/net").st_ino == host_netns
    assert _no_candidate_processes()


def test_candidate_cannot_connect_to_host_listeners(area):
    import socket

    listeners = _host_listeners()
    try:
        targets = [server.getsockname()[:2] for server in listeners]
        assert any(host == "127.0.0.1" for host, _port in targets), targets
        # Control: every listener is reachable from the host, so a refusal
        # below is the candidate's boundary, not a dead listener.
        for server, (host, port) in zip(listeners, targets):
            with socket.socket(server.family, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect((host, port))
            server.settimeout(5)
            accepted, _peer = server.accept()
            accepted.close()
            server.setblocking(False)
        source = (
            "import socket\n"
            f"targets = {targets!r}\n"
            "reached = []\n"
            "for host, port in targets:\n"
            "    family = socket.AF_INET6 if ':' in host else socket.AF_INET\n"
            "    with socket.socket(family, socket.SOCK_STREAM) as client:\n"
            "        client.settimeout(3)\n"
            "        try:\n"
            "            client.connect((host, port))\n"
            "        except OSError as exc:\n"
            "            print('denied %s errno=%s' % (host, exc.errno))\n"
            "        else:\n"
            "            reached.append(host)\n"
            "raise SystemExit(3 if reached else 0)\n"
        )
        result = _run(_python(source), area)

        assert result["passed"] is True, result
        for host, _port in targets:
            assert f"denied {host} errno=" in result["output"], result["output"]
        for server in listeners:
            # No connection from the candidate ever reached a listener.
            with pytest.raises(BlockingIOError):
                server.accept()
    finally:
        for server in listeners:
            server.close()
    assert _no_candidate_processes()


def test_candidate_cannot_resolve_names_or_egress(area):
    source = (
        "import errno, socket\n"
        "unreachable = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL,\n"
        "               errno.EAFNOSUPPORT)\n"
        "denied = 0\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 443, proto=socket.IPPROTO_TCP)\n"
        "except socket.gaierror as exc:\n"
        "    print('resolve denied', exc.errno)\n"
        "    denied += 1\n"
        "for family, target in ((socket.AF_INET, ('1.1.1.1', 443)),\n"
        "                       (socket.AF_INET, ('8.8.8.8', 53)),\n"
        "                       (socket.AF_INET6, ('2606:4700:4700::1111', 443))):\n"
        "    try:\n"
        "        # A kernel without IPv6 refuses the socket itself (EAFNOSUPPORT).\n"
        "        with socket.socket(family, socket.SOCK_STREAM) as client:\n"
        "            client.settimeout(3)\n"
        "            client.connect(target)\n"
        "    except OSError as exc:\n"
        "        print('egress denied %s errno=%s' % (target[0], exc.errno))\n"
        "        denied += exc.errno in unreachable\n"
        "with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:\n"
        "    try:\n"
        "        datagram.sendto(bytes(12), ('8.8.8.8', 53))\n"
        "    except OSError as exc:\n"
        "        print('udp denied errno=%s' % exc.errno)\n"
        "        denied += exc.errno in unreachable\n"
        "raise SystemExit(0 if denied == 5 else 3)\n"
    )
    result = _run(_python(source), area)

    assert result["passed"] is True, result
    assert "resolve denied" in result["output"]
    assert result["output"].count("egress denied") == 3
    assert "udp denied" in result["output"]
    assert _no_candidate_processes()


def _setuid_id(area: Path) -> Path:
    """A root-owned setuid copy of ``id`` that the candidate uid may execute."""
    source = shutil.which("id")
    assert source, "coreutils id is required for the setuid canary"
    target = area / "bin" / "suid-id"
    target.parent.mkdir(mode=0o755)
    shutil.copyfile(source, target)
    os.chown(target, 0, 0)
    target.chmod(0o4755)
    return target


def test_setuid_binary_cannot_raise_privileges_under_no_new_privs(area):
    import subprocess

    binary = _setuid_id(area)

    def as_candidate_without_no_new_privs():
        os.setgroups([])
        os.setresgid(CANDIDATE_UID, CANDIDATE_UID, CANDIDATE_UID)
        os.setresuid(CANDIDATE_UID, CANDIDATE_UID, CANDIDATE_UID)

    # Control: without no_new_privs the same uid gains euid 0 from this
    # binary, so the mount honours setuid and the canary discriminates.
    control = subprocess.run(
        [str(binary), "-u"], cwd=area, capture_output=True, text=True, timeout=20,
        check=False, preexec_fn=as_candidate_without_no_new_privs,
    )
    assert control.returncode == 0, control.stderr
    assert control.stdout.strip() == "0", (
        "setuid is not honoured where the canary binary lives, so the "
        f"no_new_privs canary cannot discriminate here: {control.stdout!r}"
    )

    result = _run([str(binary), "-u"], area)

    assert result["passed"] is True, result
    assert result["output"].strip() == str(CANDIDATE_UID), result["output"]
    assert result["job"]["no_new_privs"] is True
    assert _no_candidate_processes()


def test_supervisor_fails_closed_when_network_namespace_is_unavailable(area):
    """Without CAP_SYS_ADMIN the namespace cannot be made; nothing launches."""
    import ctypes
    import subprocess

    # The marker directory is writable by the candidate uid, so a missing
    # marker proves the candidate never ran rather than that it was denied.
    drop = area / "drop"
    drop.mkdir()
    drop.chmod(0o1777)
    control_marker = drop / "control-ran"
    marker = drop / "candidate-ran"

    def writes(path: Path) -> list[str]:
        return [sys.executable, "-I", "-c", f"open({str(path)!r}, 'w').write('ran')"]

    control = _run(writes(control_marker), area)
    assert control["passed"] is True, control
    assert control_marker.read_text(encoding="utf-8") == "ran"

    repo = Path(__file__).resolve().parents[1]
    driver = (
        "from scripts import selfmod_linux_isolation as linux\n"
        "try:\n"
        f"    linux.run_isolated({writes(marker)!r}, cwd={str(area)!r}, timeout=20,\n"
        f"                       candidate_uid={CANDIDATE_UID}, candidate_gid={CANDIDATE_UID})\n"
        "except linux.LinuxIsolationUnavailable as exc:\n"
        "    print('refused:', exc)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    libc = ctypes.CDLL(None, use_errno=True)

    def drop_sys_admin():
        # PR_CAPBSET_DROP (24) of CAP_SYS_ADMIN (21): the driver and its
        # reaper stay root but can no longer create a network namespace.
        if libc.prctl(24, 21, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "cannot drop CAP_SYS_ADMIN")

    completed = subprocess.run(
        [sys.executable, "-c", driver], cwd=repo, capture_output=True, text=True,
        timeout=60, check=False, preexec_fn=drop_sys_admin,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "refused: candidate launch failed: network namespace unavailable" in completed.stdout
    assert not marker.exists()
    assert _no_candidate_processes()


# A stand-in for the reaper: it enters the boundary exactly as the reaper does
# (``no_new_privs``, a fresh network namespace, the socket filter), reports it
# on stdout like the reaper's boundary event, then applies one deviation and
# blocks.  ``_confirm_boundary`` must refuse every deviation from what it
# observes in /proc, whatever the report says.
_BOUNDARY_CHILD = r"""
import ctypes, fcntl, json, os, socket, struct, sys
from scripts import selfmod_linux_isolation as linux

deviation = sys.argv[1]
libc = ctypes.CDLL(None, use_errno=True)
if deviation != "no-no-new-privs":
    assert libc.prctl(38, 1, 0, 0, 0) == 0
boundary = linux._enter_network_namespace()
keep = []
if deviation == "extra-interface":
    # A tun device (no module needed beyond tun) in the child's namespace.
    tun = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(tun, 0x400454CA, struct.pack("16sH22x", b"sondertun0", 0x0001 | 0x1000))
    keep.append(tun)
if deviation == "loopback-up":
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sH22x", b"lo", 0)
        flags = struct.unpack("16sH22x", fcntl.ioctl(probe.fileno(), 0x8913, request))[1]
        fcntl.ioctl(probe.fileno(), 0x8914, struct.pack("16sH22x", b"lo", flags | 0x1))
if deviation != "no-socket-filter":
    boundary["socket_filter"] = linux._install_socket_filter()
else:
    boundary["socket_filter"] = linux._socket_filter_report()
print(json.dumps(boundary), flush=True)
sys.stdin.read()
"""


def _boundary_child(deviation: str):
    import json
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-c", _BOUNDARY_CHILD, deviation], cwd=repo,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert child.stdout is not None
    line = child.stdout.readline()
    assert line, f"boundary child ({deviation}) exited: {child.wait(timeout=10)}"
    return child, json.loads(line)


def _stop(child) -> None:
    child.stdin.close()
    child.wait(timeout=10)


def test_supervisor_confirms_a_real_reaper_shaped_boundary():
    from scripts import selfmod_linux_isolation as linux

    child, event = _boundary_child("none")
    try:
        network = linux._confirm_boundary(child.pid, event)
    finally:
        _stop(child)
    assert network["netns_inode"] == event["netns_inode"]
    assert network["netns_inode"] != network["supervisor_netns_inode"]
    assert network["interfaces"] == ["lo"] and network["loopback_up"] is False


@pytest.mark.parametrize("deviation, match", [
    ("extra-interface", "has interfaces"),
    ("loopback-up", "loopback up"),
    ("no-no-new-privs", "no_new_privs"),
    ("no-socket-filter", "socket filter"),
])
def test_supervisor_refuses_what_proc_contradicts_even_when_the_report_is_clean(deviation, match):
    """Each deviation is observed from /proc; the (clean) report is not trusted."""
    from scripts import selfmod_linux_isolation as linux

    child, event = _boundary_child(deviation)
    try:
        # The report claims the clean boundary in every case.
        assert event["interfaces"] == ["lo"] and event["loopback_up"] is False
        with pytest.raises(linux.LinuxIsolationUnavailable, match=match):
            linux._confirm_boundary(child.pid, event)
    finally:
        _stop(child)


@pytest.mark.parametrize("forged, match", [
    ({"netns_inode": "other"}, "does not match"),
    ({"interfaces": ["eth0", "lo"]}, "has interfaces"),
    ({"loopback_up": True}, "loopback up"),
    ({"socket_filter": {"mechanism": "seccomp", "socket_families": [1, 2, 10, 16, 40],
                        "io_uring": "denied"}}, "socket filter"),
])
def test_supervisor_refuses_a_report_that_disagrees_with_proc(forged, match):
    """The child really is in its own namespace, so only the mismatch can refuse."""
    from scripts import selfmod_linux_isolation as linux

    child, event = _boundary_child("none")
    try:
        if forged.get("netns_inode") == "other":
            forged = {"netns_inode": event["netns_inode"] + 1}
        with pytest.raises(linux.LinuxIsolationUnavailable, match=match):
            linux._confirm_boundary(child.pid, {**event, **forged})
        # The unforged report of the same child is confirmed.
        linux._confirm_boundary(child.pid, event)
    finally:
        _stop(child)


def test_boundary_sharing_the_supervisor_network_namespace_is_refused():
    from scripts import selfmod_linux_isolation as linux

    # This process is in the supervisor's own namespace, so a report naming
    # it is refused on the shared inode before anything else is compared.
    own = os.stat("/proc/self/ns/net").st_ino
    event = {"netns_inode": own, "interfaces": ["lo"], "loopback_up": False,
             "socket_filter": linux._socket_filter_report()}
    with pytest.raises(linux.LinuxIsolationUnavailable, match="shares the supervisor"):
        linux._confirm_boundary(os.getpid(), event)


# Families a network namespace does not scope (AF_VSOCK reaches a VM's
# hypervisor host from any namespace) or that only widen kernel surface.
_FOREIGN_FAMILIES = {"AF_VSOCK": (40, 1), "AF_PACKET": (17, 3), "AF_BLUETOOTH": (31, 3),
                     "AF_ALG": (38, 5), "AF_TIPC": (30, 5), "AF_CAN": (29, 3)}
_FAMILY_PROBE = r"""
import ctypes, errno, json, socket, sys
families = json.loads(sys.argv[1])
report = {}
for name, (family, kind) in families.items():
    try:
        socket.socket(family, kind).close()
    except OSError as exc:
        report[name] = exc.errno
    else:
        report[name] = 0
libc = ctypes.CDLL(None, use_errno=True)
params = ctypes.create_string_buffer(120)
fd = libc.syscall(425, 1, params)
report["io_uring"] = 0 if fd >= 0 else ctypes.get_errno()
a, b = socket.socketpair()
a.close(); b.close()
for family, kind in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET6, socket.SOCK_STREAM),
                     (socket.AF_NETLINK, socket.SOCK_RAW)):
    try:
        socket.socket(family, kind).close()
    except OSError as exc:
        report["allowed %d" % family] = exc.errno
    else:
        report["allowed %d" % family] = 0
print(json.dumps(report))
"""


def test_candidate_cannot_open_address_families_the_namespace_does_not_scope(area):
    import ctypes
    import errno
    import json
    import subprocess

    families = json.dumps(_FOREIGN_FAMILIES)
    libc = ctypes.CDLL(None, use_errno=True)

    def as_candidate_in_netns_without_filter():
        os.unshare(os.CLONE_NEWNET)
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs")
        os.setgroups([])
        os.setresgid(CANDIDATE_UID, CANDIDATE_UID, CANDIDATE_UID)
        os.setresuid(CANDIDATE_UID, CANDIDATE_UID, CANDIDATE_UID)

    # Control: the same uid in its own network namespace under no_new_privs,
    # but without the socket filter.  Every family the kernel offers gives
    # something other than EAFNOSUPPORT here, so the canary discriminates.
    control_run = subprocess.run(
        [sys.executable, "-I", "-c", _FAMILY_PROBE, families], cwd=area,
        capture_output=True, text=True, timeout=20, check=False,
        preexec_fn=as_candidate_in_netns_without_filter,
    )
    assert control_run.returncode == 0, control_run.stderr
    control = json.loads(control_run.stdout)
    # AF_PACKET is built into every mainstream kernel: without the filter it
    # is refused for want of CAP_NET_RAW, never as an unsupported family.
    assert control["AF_PACKET"] == errno.EPERM, control
    assert control["io_uring"] != errno.ENOSYS, control
    offered = sorted(name for name in _FOREIGN_FAMILIES if control[name] != errno.EAFNOSUPPORT)
    if os.path.exists("/dev/vsock"):
        # A VM guest with a vsock transport: the namespace alone leaves the
        # hypervisor host reachable, which is what the filter closes.
        assert control["AF_VSOCK"] == 0, control

    result = _run([sys.executable, "-I", "-c", _FAMILY_PROBE, families], area)

    assert result["passed"] is True, result
    candidate = json.loads(result["output"].strip().splitlines()[-1])
    for name in _FOREIGN_FAMILIES:
        assert candidate[name] == errno.EAFNOSUPPORT, (name, candidate, offered)
    assert candidate["io_uring"] == errno.ENOSYS, candidate
    # The namespace-scoped families stay usable (IPv6 may be absent).
    assert candidate["allowed 2"] == 0 and candidate["allowed 16"] == 0, candidate
    assert candidate["allowed 10"] in {0, errno.EAFNOSUPPORT}, candidate
    assert candidate["allowed 10"] == control["allowed 10"], (candidate, control)
    assert result["job"]["socket_filter"] == {
        "mechanism": "seccomp", "socket_families": [1, 2, 10, 16], "io_uring": "denied",
    }
    assert result["attestation"].socket_families_filtered is True
    assert _no_candidate_processes()


def test_candidate_entering_the_kernel_through_a_foreign_abi_is_killed(area):
    import platform
    import signal

    from scripts import selfmod_linux_isolation as linux

    program = linux._socket_filter_program()
    audit_arch = linux._SECCOMP_ABIS[platform.machine()][0]
    # The first check of every program: a foreign audit arch jumps to kill.
    assert program[0] == (linux._BPF_LD_W_ABS, 0, 0, linux._SECCOMP_ARCH)
    assert program[1][0] == linux._BPF_JEQ_K and program[1][3] == audit_arch
    assert program[1 + 1 + program[1][2]] == (linux._BPF_RET_K, 0, 0,
                                              linux._SECCOMP_RET_KILL_PROCESS)
    if platform.machine() != "x86_64":
        return
    # x86_64 also accepts x32 syscall numbers under the native audit arch;
    # getpid through the x32 table must kill the process, not run.
    source = (
        "import ctypes\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "print('native getpid', libc.syscall(39) > 0, flush=True)\n"
        "libc.syscall(0x40000000 | 39)\n"
        "print('x32 syscall ran', flush=True)\n"
    )
    result = _run(_python(source), area)

    assert "native getpid True" in result["output"], result
    assert "x32 syscall ran" not in result["output"], result
    assert result["job"]["exit"]["signal"] == signal.SIGSYS, result["job"]
    assert result["passed"] is False
    assert _no_candidate_processes()


def test_fallback_tunnel_interfaces_are_refused_with_the_sysctl_to_change():
    """A kernel that adds fallback tunnels to new namespaces is named, not guessed at."""
    import subprocess

    repo = Path(__file__).resolve().parents[1]
    source = (
        "import socket\n"
        "from scripts import selfmod_linux_isolation as linux\n"
        "socket.if_nameindex = lambda: [(1, 'lo'), (2, 'tunl0'), (3, 'sit0')]\n"
        "try:\n"
        "    linux._enter_network_namespace()\n"
        "except OSError as exc:\n"
        "    print('refused:', exc)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(3)\n"
    )
    completed = subprocess.run([sys.executable, "-c", source], cwd=repo, capture_output=True,
                               text=True, timeout=30, check=False)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "['lo', 'sit0', 'tunl0']" in completed.stdout
    assert "net.core.fb_tunnels_only_for_init_net=1" in completed.stdout


def test_supervisor_refuses_before_launch_when_fallback_tunnels_are_expected(area, monkeypatch):
    from scripts import selfmod_linux_isolation as linux

    sysctl = area / "fb_tunnels_only_for_init_net"
    sysctl.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(linux, "_FB_TUNNELS_SYSCTL", sysctl)
    monkeypatch.setattr(linux.socket, "if_nameindex", lambda: [(1, "lo"), (7, "tunl0")])
    monkeypatch.setattr(linux.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(linux.LinuxIsolationUnavailable, match="fb_tunnels_only_for_init_net=1"):
        _run(_python("pass"), area)
    # The nightly preflight refuses on the same grounds before any run exists.
    monkeypatch.setenv(linux.CANDIDATE_UID_ENV, str(CANDIDATE_UID))
    assert "fb_tunnels_only_for_init_net=1" in (linux.candidate_isolation_preflight() or "")
    # With the sysctl set the host is not refused on this account.
    sysctl.write_text("1\n", encoding="utf-8")
    assert linux._fallback_tunnels_expected() is False
    sysctl.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(linux.socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    assert linux._fallback_tunnels_expected() is False
