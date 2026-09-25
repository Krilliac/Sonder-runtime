"""Linux uid-separated supervisor for unattended self-mod checks (#517).

This is the Linux counterpart of ``scripts/selfmod_low_integrity.py`` and
returns the same result schema.  The supervisor stays root and owns the
evaluator truth.  Candidate code runs as a distinct, dedicated, unprivileged
uid/gid with no supplementary groups and ``no_new_privs``, in its own session,
below per-process rlimits, with a scrubbed environment and a private
HOME/TMPDIR.  Protected evaluator truth must not be writable by that uid: the
supervisor proves this before launch (failing closed otherwise) and re-digests
it after the candidate is gone.

Process topology::

    supervisor (root, this process)
      '-- reaper (root, ``--reaper``; child subreaper, dies with supervisor)
            '-- candidate (candidate uid, new session) and every descendant

The reaper exists so that orphaned candidate descendants are reaped instead of
lingering as zombies that count against the candidate uid's RLIMIT_NPROC.  The
supervisor tears the candidate down by uid, not by process group: every live
process whose real uid is the dedicated candidate uid belongs to the candidate
(the supervisor refuses to launch while any exist), so a grandchild that
called ``setsid()`` cannot escape.

What this boundary does NOT provide (see
``docs/architecture/REMAINING-SELFMOD-517-LINUX-ISOLATION.md``): the candidate
still produces the output the parent grades (result-frame forgery), network
access is not isolated, confidentiality of world-readable files is not
provided, and job memory is enforced by sampling rather than a cgroup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ATTESTATION = "linux-uid"
CANDIDATE_UID_ENV = "SONDER_SELFMOD_CANDIDATE_UID"
CANDIDATE_GID_ENV = "SONDER_SELFMOD_CANDIDATE_GID"
ISOLATION_DOC = "docs/architecture/REMAINING-SELFMOD-517-LINUX-ISOLATION.md"
# The one operator-facing explanation for a Linux host that has not been
# provisioned for unattended candidate checks.  It names what to set and
# where the requirements are documented, so a refusal is actionable.
UNCONFIGURED_GUIDANCE = (
    f"Linux candidate isolation is not configured: set {CANDIDATE_UID_ENV} to a "
    f"dedicated, otherwise unused, unprivileged uid (optionally {CANDIDATE_GID_ENV}) "
    f"and run the selfmod supervisor as root; see {ISOLATION_DOC}"
)

_MIB = 1024 ** 2
DEFAULT_PROCESS_MEMORY_MB = 2048
DEFAULT_JOB_MEMORY_MB = 4096
DEFAULT_ACTIVE_PROCESSES = 32
DEFAULT_FILE_SIZE_MB = 1024
# Hard ceilings match the Windows supervisor so a misconfigured caller cannot
# turn the bounds into unbounded ones.
_MAX_PROCESS_MEMORY_MB = 16384
_MAX_JOB_MEMORY_MB = 24576
_MAX_ACTIVE_PROCESSES = 128
_MAX_TIMEOUT_SECONDS = 24 * 3600
# Grace beyond the wall-clock deadline before RLIMIT_CPU delivers SIGXCPU and
# then SIGKILL to a single process that spins instead of sleeping.
_CPU_GRACE_SECONDS = 5
_OUTPUT_TAIL_BYTES = 120_000
_SAMPLE_INTERVAL_SECONDS = 0.1
_TEARDOWN_SECONDS = 10.0
_REAPER_EXIT_SECONDS = 10.0
_MAX_PROTECTED_ENTRIES = 20_000
_REAPER_BUFFER_BYTES = 64 * 1024
# Root-only directory holding one flock per candidate uid.  Two supervisors
# sharing a uid would let one candidate ptrace or signal the other's process
# (same uid) and would make each teardown kill the other's tree, so the
# spare-uid check and the whole run happen under an exclusive claim.
_CLAIM_DIR = Path("/run/sonder-selfmod-candidate")

# prctl(2) options (linux/prctl.h); stable kernel ABI.
_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39

# Non-secret locale facts only.  Credentials, tokens, SSH agents, proxies and
# the supervisor's real HOME are never forwarded.
_PASSTHROUGH_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class LinuxIsolationUnavailable(RuntimeError):
    """The Linux uid boundary cannot be established; the check must not run.

    Raised when the host is not Linux, the supervisor is not root, no
    dedicated unprivileged candidate uid is configured or it is in use, or
    the kernel lacks a required facility.  Callers map it to an isolation
    failure (``selfmod`` records exit code 125); it is never a pass.
    """


class ProtectedPathExposed(LinuxIsolationUnavailable):
    """A protected path, an ancestor or an entry is candidate-writable or a symlink."""


def candidate_supervisor() -> tuple[Callable[..., dict[str, object]], str]:
    """Return the host's candidate supervisor and the attestation it builds.

    On Linux the uid supervisor is selected once an operator has provisioned
    a dedicated candidate uid in ``SONDER_SELFMOD_CANDIDATE_UID``.  Otherwise
    the historical Windows low-integrity supervisor is returned; on a
    non-Windows host it fails closed.  Callers must accept only the returned
    attestation, so neither supervisor can vouch for the other's boundary.
    """
    if sys.platform.startswith("linux") and os.environ.get(CANDIDATE_UID_ENV, "").strip():
        return run_isolated, ATTESTATION
    from scripts import selfmod_low_integrity

    return selfmod_low_integrity.run_isolated, "low"


def candidate_isolation_preflight() -> str | None:
    """Say why this host cannot isolate unattended candidate checks, or ``None``.

    Callers that are about to create a selfmod run (``nightly_selfmod``) use
    this to refuse before any run, backup or workspace exists.  The checks
    are host facts only: Linux, a root supervisor, ``no_new_privs``, and a
    configured, unprivileged, currently spare candidate uid.  ``run_isolated``
    re-checks all of them under the exclusive uid claim for every candidate
    command, so passing preflight never authorizes a launch by itself.

    On Windows the low-integrity supervisor proves its own boundary for each
    command, so there is nothing to pre-check here.
    """
    if os.name == "nt":
        return None
    if not sys.platform.startswith("linux"):
        return (
            f"unsupported platform: no selfmod candidate supervisor exists for "
            f"{sys.platform}; see {ISOLATION_DOC}"
        )
    if not os.environ.get(CANDIDATE_UID_ENV, "").strip():
        return UNCONFIGURED_GUIDANCE
    try:
        _require_host()
        uid, gid = candidate_identity(None, None)
        _require_spare_identity(uid, gid)
    except LinuxIsolationUnavailable as exc:
        return f"{exc}; see {ISOLATION_DOC}"
    return None


def require_not_candidate_writable(paths: Sequence[str | os.PathLike[str]]) -> None:
    """Refuse when the configured candidate uid could write any of ``paths``.

    For host state the parent legitimately mutates between candidate checks
    (the selfmod ledger with its baseline, tested-byte digests and
    decisions), a before/after content digest would flag the parent's own
    writes.  This applies the same pre-launch exposure rules as
    ``protected_paths`` (no symlink, no candidate-owned or -writable ancestor,
    no ACL) without the content comparison.  Raises
    ``LinuxIsolationUnavailable``/``ProtectedPathExposed``.
    """
    _require_host()
    uid, gid = candidate_identity(None, None)
    _verify_protected([Path(item) for item in paths], uid, gid)


def _attested(result: dict[str, object]) -> dict[str, object]:
    """Attach the typed isolation attestation built from this supervisor's report."""
    from sonder_runtime.application.selfmod.candidate_isolation import IsolationAttestation

    result["attestation"] = IsolationAttestation.from_supervisor_result(
        result, expected_kind=ATTESTATION, supervisor_uid=os.geteuid(),
    )
    return result


def candidate_identity(uid: int | None, gid: int | None) -> tuple[int, int]:
    """Resolve the dedicated candidate uid/gid (explicit value, then environment)."""
    try:
        if uid is None:
            configured = os.environ.get(CANDIDATE_UID_ENV, "").strip()
            if not configured:
                raise LinuxIsolationUnavailable(
                    f"no dedicated candidate uid is configured ({CANDIDATE_UID_ENV}); "
                    + UNCONFIGURED_GUIDANCE
                )
            uid = int(configured)
        if gid is None:
            configured = os.environ.get(CANDIDATE_GID_ENV, "").strip()
            gid = int(configured) if configured else int(uid)
    except ValueError as exc:
        raise LinuxIsolationUnavailable("candidate uid/gid must be integers") from exc
    return int(uid), int(gid)


def _bounded(value: int | None, default: int, ceiling: int, name: str) -> int:
    number = default if value is None else int(value)
    if number < 1 or number > ceiling:
        raise ValueError(f"{name} must be between 1 and {ceiling}")
    return number


def _proc_status(pid: int) -> dict[str, str]:
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields = {}
    for line in text.splitlines():
        key, _sep, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def _uid_processes(uid: int) -> dict[int, dict[str, str]]:
    """Map pid -> /proc status for every live (non-zombie) process of real ``uid``."""
    found: dict[int, dict[str, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fields = _proc_status(int(entry))
        uids = fields.get("Uid", "").split()
        if not uids or int(uids[0]) != uid:
            continue
        if fields.get("State", "").startswith(("Z", "X")):
            continue
        found[int(entry)] = fields
    return found


def live_uid_pids(uid: int) -> set[int]:
    """Return the pids of live processes whose real uid is ``uid``."""
    return set(_uid_processes(uid))


def _kib(value: str) -> int:
    parts = value.split()
    return int(parts[0]) if parts and parts[0].isdigit() else 0


def _kill_uid(uid: int, *, seconds: float = _TEARDOWN_SECONDS) -> int:
    """SIGKILL every process of ``uid`` until none remain; return how many."""
    killed: set[int] = set()
    deadline = time.monotonic() + seconds
    while True:
        pids = live_uid_pids(uid)
        if not pids:
            return len(killed)
        if time.monotonic() > deadline:
            raise RuntimeError("candidate processes survived teardown")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            killed.add(pid)
        time.sleep(0.02)


def _has_acl(path: Path) -> bool:
    """POSIX ACLs can grant write beyond the mode bits; treat them as exposure."""
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return False
    for name in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            getxattr(path, name, follow_symlinks=False)
        except OSError:
            continue
        return True
    return False


def _candidate_may_write(info: os.stat_result, uid: int, gid: int) -> bool:
    """Evaluate write permission for the candidate credentials (no groups, no caps)."""
    if info.st_uid == uid:
        # An owner can always chmod its way to write access.
        return True
    if info.st_gid == gid:
        return bool(info.st_mode & stat.S_IWGRP)
    return bool(info.st_mode & stat.S_IWOTH)


def _check_entry(path: Path, info: os.stat_result, uid: int, gid: int, child_owner: int | None) -> None:
    if _has_acl(path):
        raise ProtectedPathExposed(f"protected chain carries a POSIX ACL: {path}")
    if info.st_uid == uid:
        raise ProtectedPathExposed(f"candidate uid owns protected chain entry: {path}")
    if not _candidate_may_write(info, uid, gid):
        return
    # A sticky, world-writable directory (``/tmp``) lets the candidate create
    # entries but not rename or unlink one it does not own; the next entry on
    # the protected chain has already been checked not to be candidate-owned.
    if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX and child_owner is not None:
        return
    raise ProtectedPathExposed(f"protected chain entry is candidate-writable: {path}")


def _chain(path: Path) -> list[Path]:
    """Every directory from ``/`` down to ``path``'s parent, then ``path``."""
    return [*reversed(path.parents), path]


def _verify_protected(paths: Sequence[Path], uid: int, gid: int) -> dict[str, object]:
    """Refuse candidate-writable truth and return a digest snapshot of it.

    Each protected path, every ancestor directory (both as written and as
    resolved) and, for a directory, every entry inside it must be neither
    candidate-owned nor candidate-writable.  A protected path that is itself a
    symlink is refused: the candidate-visible name would not be what was
    digested.  A missing path is recorded as missing; its parent chain still
    has to be closed so the candidate cannot create it.
    """
    snapshot: dict[str, object] = {}
    budget = [_MAX_PROTECTED_ENTRIES]
    for raw in paths:
        literal = Path(os.path.abspath(os.fspath(raw)))
        try:
            final = literal.lstat()
        except FileNotFoundError:
            final = None
        if final is not None and stat.S_ISLNK(final.st_mode):
            raise ProtectedPathExposed(f"protected path is a symlink: {literal}")
        chains = [_chain(literal)]
        resolved = literal.resolve()
        if resolved != literal:
            chains.append(_chain(resolved))
        for chain in chains:
            for index, entry in enumerate(chain):
                try:
                    info = entry.lstat()
                except FileNotFoundError:
                    if entry == chain[-1]:
                        continue
                    raise ProtectedPathExposed(f"protected ancestor is missing: {entry}") from None
                child_owner = None
                if index + 1 < len(chain):
                    try:
                        child_owner = chain[index + 1].lstat().st_uid
                    except FileNotFoundError:
                        child_owner = None
                if stat.S_ISLNK(info.st_mode):
                    # The literal chain may pass through a root-owned
                    # symlink; the resolved chain covers its target.  The
                    # link itself must not be candidate-owned.
                    if info.st_uid == uid:
                        raise ProtectedPathExposed(f"candidate uid owns protected chain link: {entry}")
                    continue
                _check_entry(entry, info, uid, gid, child_owner)
        snapshot[str(literal)] = _snapshot(literal, uid, gid, budget)
        snapshot["ancestors:" + str(literal)] = [
            _metadata(item) for item in _chain(resolved)[:-1]
        ]
    return snapshot


def _metadata(path: Path) -> list[int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return [info.st_uid, info.st_gid, info.st_mode, info.st_ino]


def _snapshot(path: Path, uid: int, gid: int, budget: list[int]) -> object:
    """Digest file bytes plus ownership/mode, recursing through directories."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing"
    budget[0] -= 1
    if budget[0] < 0:
        raise ProtectedPathExposed(f"protected tree exceeds {_MAX_PROTECTED_ENTRIES} entries")
    meta = [info.st_uid, info.st_gid, info.st_mode, info.st_ino]
    if stat.S_ISLNK(info.st_mode):
        raise ProtectedPathExposed(f"protected tree contains a symlink: {path}")
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"meta": meta, "sha256": digest.hexdigest()}
    if stat.S_ISDIR(info.st_mode):
        entries = {}
        for child in sorted(os.listdir(path)):
            child_path = path / child
            child_info = child_path.lstat()
            if not stat.S_ISLNK(child_info.st_mode):
                _check_entry(child_path, child_info, uid, gid, None)
            entries[child] = _snapshot(child_path, uid, gid, budget)
        return {"meta": meta, "entries": entries}
    raise ProtectedPathExposed(f"protected path is not a regular file or directory: {path}")


def _require_host() -> None:
    if not sys.platform.startswith("linux"):
        raise LinuxIsolationUnavailable("linux-uid selfmod isolation requires Linux")
    if os.geteuid() != 0:
        raise LinuxIsolationUnavailable(
            "linux-uid selfmod isolation requires the supervisor to run as root"
        )
    if "NoNewPrivs" not in _proc_status(os.getpid()):
        raise LinuxIsolationUnavailable("kernel lacks no_new_privs or /proc is unavailable")


def _require_spare_identity(uid: int, gid: int) -> None:
    if uid <= 0 or gid <= 0:
        raise LinuxIsolationUnavailable("candidate uid/gid must be unprivileged (non-zero)")
    if uid in os.getresuid() or gid in os.getresgid():
        raise LinuxIsolationUnavailable("candidate uid/gid must differ from the supervisor")
    if live_uid_pids(uid):
        raise LinuxIsolationUnavailable(
            f"candidate uid {uid} is not spare: processes already run as it"
        )


def _claim_identity(uid: int) -> int:
    """Take the exclusive per-uid claim; return its fd (release by closing).

    The claim lives in a root-owned, not group/other-writable directory so
    that no candidate can pre-create or hold it.  A busy claim fails closed.
    """
    import fcntl

    try:
        _CLAIM_DIR.mkdir(mode=0o700, exist_ok=True)
        info = _CLAIM_DIR.lstat()
    except OSError as exc:
        raise LinuxIsolationUnavailable(
            f"cannot create candidate claim directory {_CLAIM_DIR}: {type(exc).__name__}"
        ) from exc
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise LinuxIsolationUnavailable(f"candidate claim directory is not root-only: {_CLAIM_DIR}")
    try:
        fd = os.open(_CLAIM_DIR / f"{uid}.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise LinuxIsolationUnavailable(f"cannot open candidate claim: {type(exc).__name__}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LinuxIsolationUnavailable(
            f"candidate uid {uid} is claimed by another supervisor run"
        ) from None
    return fd


def _candidate_environment(home: Path, tmp: Path) -> dict[str, str]:
    env = {"PATH": _DEFAULT_PATH}
    for name in _PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env.update({
        "HOME": str(home), "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
        "USER": "sonder-candidate", "LOGNAME": "sonder-candidate",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(tmp / "pycache"),
    })
    return env


class _Drain:
    """Continuously drain the candidate output pipe into a bounded tail."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._tail = bytearray()
        self.error = ""
        self._thread = threading.Thread(target=self._run, name="selfmod-linux-drain", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(16 * 1024)
                if not chunk:
                    break
                self._tail += chunk
                if len(self._tail) > _OUTPUT_TAIL_BYTES:
                    del self._tail[:-_OUTPUT_TAIL_BYTES]
        except (OSError, ValueError) as exc:
            self.error = type(exc).__name__
        finally:
            self._stream.close()

    def join(self, seconds: float) -> bool:
        self._thread.join(timeout=seconds)
        return not self._thread.is_alive()

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", "replace")


def _read_events(fd: int, buffer: bytearray, events: list[dict[str, Any]]) -> bool:
    """Read available reaper events; return False once the pipe is closed."""
    try:
        chunk = os.read(fd, _REAPER_BUFFER_BYTES)
    except BlockingIOError:
        return True
    if not chunk:
        return False
    buffer += chunk
    while b"\n" in buffer:
        line, _sep, rest = bytes(buffer).partition(b"\n")
        buffer[:] = rest
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return True


def run_isolated(
    command: Sequence[str], *, cwd: str | os.PathLike[str], timeout: int,
    protected_paths: Sequence[str | os.PathLike[str]] = (),
    process_memory_mb: int | None = None, job_memory_mb: int | None = None,
    active_processes: int | None = None,
    candidate_uid: int | None = None, candidate_gid: int | None = None,
) -> dict[str, object]:
    """Run ``command`` as the dedicated candidate uid; return a selfmod result.

    The result has the Windows supervisor's schema: ``exit_code``, bounded
    ``output``, ``passed`` and a supervisor-built ``job`` attestation with
    ``integrity='linux-uid'``; ``integrity_failed`` is set when protected
    truth changed.  Candidate stdout never contributes to the attestation.

    Limits: ``process_memory_mb`` is RLIMIT_AS per process,
    ``active_processes`` is RLIMIT_NPROC for the candidate uid, and
    ``job_memory_mb`` bounds the summed resident memory of all candidate
    processes, enforced by supervisor sampling (not atomically).  RLIMIT_CPU
    and RLIMIT_FSIZE bound runaway CPU and file growth.

    Raises ``LinuxIsolationUnavailable`` (or ``ProtectedPathExposed``) before
    the candidate launches whenever the boundary cannot be established.
    """
    _require_host()
    uid, gid = candidate_identity(candidate_uid, candidate_gid)
    if uid <= 0 or gid <= 0:
        raise LinuxIsolationUnavailable("candidate uid/gid must be unprivileged (non-zero)")
    claim = _claim_identity(uid)
    try:
        return _attested(_run_claimed(
            command, cwd=cwd, timeout=timeout, protected_paths=protected_paths,
            process_memory_mb=process_memory_mb, job_memory_mb=job_memory_mb,
            active_processes=active_processes, uid=uid, gid=gid,
        ))
    finally:
        os.close(claim)


def _run_claimed(
    command: Sequence[str], *, cwd: str | os.PathLike[str], timeout: int,
    protected_paths: Sequence[str | os.PathLike[str]],
    process_memory_mb: int | None, job_memory_mb: int | None,
    active_processes: int | None, uid: int, gid: int,
) -> dict[str, object]:
    _require_spare_identity(uid, gid)
    process_memory_mb = _bounded(process_memory_mb, DEFAULT_PROCESS_MEMORY_MB, _MAX_PROCESS_MEMORY_MB, "process_memory_mb")
    job_memory_mb = _bounded(job_memory_mb, DEFAULT_JOB_MEMORY_MB, _MAX_JOB_MEMORY_MB, "job_memory_mb")
    active_processes = _bounded(active_processes, DEFAULT_ACTIVE_PROCESSES, _MAX_ACTIVE_PROCESSES, "active_processes")
    seconds = max(1, min(int(timeout), _MAX_TIMEOUT_SECONDS))
    protected = [Path(item) for item in protected_paths]
    before = _verify_protected(protected, uid, gid)

    work = Path(tempfile.mkdtemp(prefix="sonder-linux-cand-"))
    try:
        work.chmod(0o711)
        control = work / "ctl"
        control.mkdir(mode=0o700)
        home = work / "home"
        tmp = work / "tmp"
        for private in (home, tmp):
            private.mkdir(mode=0o700)
            os.chown(private, uid, gid)
        limits = {
            "process_memory_mb": process_memory_mb, "job_memory_mb": job_memory_mb,
            "active_processes": active_processes,
            "cpu_seconds": seconds + _CPU_GRACE_SECONDS,
            "file_size_mb": DEFAULT_FILE_SIZE_MB,
            "job_memory_enforcement": "sampled-rss",
        }
        spec_path = control / "spec.json"
        spec_path.write_text(json.dumps({
            "command": [str(item) for item in command],
            "cwd": str(Path(cwd).resolve()), "env": _candidate_environment(home, tmp),
            "uid": uid, "gid": gid, "limits": limits,
        }), encoding="utf-8")
        return _supervise(spec_path, control, uid, gid, seconds, limits, protected, before)
    finally:
        try:
            _kill_uid(uid)
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _supervise(
    spec_path: Path, control: Path, uid: int, gid: int, seconds: int,
    limits: dict[str, object], protected: Sequence[Path], before: dict[str, object],
) -> dict[str, object]:
    read_fd, write_fd = os.pipe()
    reaper = None
    drain = None
    try:
        os.set_blocking(read_fd, False)
        # ``-I`` keeps the candidate checkout (the reaper's eventual child
        # cwd) and user site-packages from shadowing this trusted file.
        reaper = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--reaper",
             str(spec_path), str(write_fd), str(os.getpid())],
            cwd=str(control), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, pass_fds=(write_fd,), start_new_session=True,
            env={"PATH": _DEFAULT_PATH},
        )
        os.close(write_fd)
        write_fd = -1
        assert reaper.stdout is not None
        drain = _Drain(reaper.stdout)
        events: list[dict[str, Any]] = []
        buffer = bytearray()
        open_pipe = True
        started = time.monotonic()
        deadline = started + seconds
        # Past this point the reaper itself is unresponsive; stop waiting.
        hard_deadline = deadline + _TEARDOWN_SECONDS + _REAPER_EXIT_SECONDS
        timed_out = False
        limit_hit = None
        peak_job_kib = peak_process_kib = peak_processes = 0
        exit_event = None
        while exit_event is None and open_pipe and time.monotonic() < hard_deadline:
            ready, _w, _x = select.select([read_fd], [], [], _SAMPLE_INTERVAL_SECONDS)
            if ready:
                open_pipe = _read_events(read_fd, buffer, events)
            exit_event = next((e for e in events if e.get("event") in {"exit", "launch_error"}), None)
            if exit_event is not None:
                break
            processes = _uid_processes(uid)
            peak_processes = max(peak_processes, len(processes))
            job_kib = sum(_kib(fields.get("VmRSS", "")) for fields in processes.values())
            peak_job_kib = max(peak_job_kib, job_kib)
            for fields in processes.values():
                peak_process_kib = max(peak_process_kib, _kib(fields.get("VmHWM", "")))
            if job_kib > int(limits["job_memory_mb"]) * 1024:
                limit_hit = "job_memory"
                _kill_uid(uid)
            elif time.monotonic() >= deadline:
                timed_out = True
                _kill_uid(uid)
        # The candidate's lifetime ends with its main process: anything it
        # left behind (including setsid() grandchildren) is torn down now.
        lingering = _kill_uid(uid)
        reaper_deadline = time.monotonic() + _REAPER_EXIT_SECONDS
        while open_pipe and time.monotonic() < reaper_deadline:
            ready, _w, _x = select.select([read_fd], [], [], 0.1)
            if ready:
                open_pipe = _read_events(read_fd, buffer, events)
        try:
            reaper.wait(timeout=max(0.1, reaper_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reaper.kill()
            reaper.wait(timeout=5)
            raise RuntimeError("selfmod reaper did not terminate") from None
        if not drain.join(10):
            raise RuntimeError("candidate output drain did not terminate")
        exit_event = next((e for e in events if e.get("event") in {"exit", "launch_error"}), None)
        if exit_event is None or exit_event.get("event") == "launch_error":
            reason = "no exit report" if exit_event is None else str(exit_event.get("error", ""))[:300]
            raise LinuxIsolationUnavailable(f"candidate launch failed: {reason}")
        done = next((e for e in events if e.get("event") == "done"), {})
        status = int(exit_event["status"])
        signal_number = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        returncode = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + int(signal_number or 0)
        peak_process_kib = max(peak_process_kib, int(done.get("children_maxrss_kib", 0) or 0))
        job_report: dict[str, object] = {
            "integrity": ATTESTATION, "uid": uid, "gid": gid,
            "supervisor_uid": os.geteuid(), "limits": dict(limits),
            "exit": {"returncode": returncode, "signal": signal_number},
            "timed_out": timed_out, "limit_hit": limit_hit,
            "peak_process_memory_mb": peak_process_kib // 1024,
            "peak_job_memory_mb": peak_job_kib // 1024,
            "peak_processes": peak_processes,
            "lingering_processes_killed": lingering,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        output = drain.text()
        if drain.error:
            raise RuntimeError(f"candidate output drain failed: {drain.error}")
        after = _snapshot_after(protected, uid, gid)
        if after != before:
            return {"exit_code": 2, "passed": False, "integrity_failed": True, "job": job_report,
                    "output": output + "\nSELFMOD EVALUATOR CANARY FAILED: protected truth changed\n"}
        code = 124 if timed_out else returncode
        if timed_out:
            output += f"\nSELFMOD LINUX TIMEOUT: candidate exceeded {seconds}s; process tree killed\n"
        return {"exit_code": int(code), "output": output[-_OUTPUT_TAIL_BYTES:],
                "passed": int(code) == 0, "job": job_report}
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)
        if reaper is not None and reaper.poll() is None:
            reaper.kill()
            reaper.wait(timeout=5)
        if drain is not None:
            drain.join(5)


def _snapshot_after(protected: Sequence[Path], uid: int, gid: int) -> dict[str, object] | None:
    """Re-verify and re-digest protected truth; any exposure counts as changed."""
    try:
        return _verify_protected(protected, uid, gid)
    except (ProtectedPathExposed, OSError):
        return None


def _set_limits(limits: dict[str, Any]) -> None:
    import resource

    bounds = (
        (resource.RLIMIT_AS, int(limits["process_memory_mb"]) * _MIB),
        (resource.RLIMIT_NPROC, int(limits["active_processes"])),
        (resource.RLIMIT_CPU, int(limits["cpu_seconds"])),
        (resource.RLIMIT_FSIZE, int(limits["file_size_mb"]) * _MIB),
        (resource.RLIMIT_CORE, 0),
    )
    for kind, value in bounds:
        resource.setrlimit(kind, (value, value))


def _reaper(spec_path: Path, event_fd: int, supervisor_pid: int) -> int:
    """Launch the candidate below the uid boundary and reap all descendants."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.restype = ctypes.c_int
    events = os.fdopen(event_fd, "w", encoding="utf-8", buffering=1)

    def emit(**event: object) -> None:
        events.write(json.dumps(event) + "\n")

    if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != supervisor_pid:
        emit(event="launch_error", error="supervisor is gone")
        return 125
    if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        emit(event="launch_error", error="child subreaper unsupported")
        return 125
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    uid, gid = int(spec["uid"]), int(spec["gid"])
    cwd = str(spec["cwd"])
    reaper_pid = os.getpid()

    def drop_privileges() -> None:
        # Runs in the forked child before exec.  Order matters: bound the
        # process while still root, forbid privilege gain, then drop groups,
        # gid and finally uid, and prove the drop cannot be undone.
        _set_limits(spec["limits"])
        if prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
            raise OSError(ctypes.get_errno(), "no_new_privs unavailable")
        os.setgroups([])
        os.setresgid(gid, gid, gid)
        os.setresuid(uid, uid, uid)
        if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid) or os.getgroups():
            raise OSError("candidate credentials did not drop")
        try:
            os.setresuid(0, 0, 0)
        except PermissionError:
            pass
        else:
            raise OSError("candidate regained root")
        # Credential changes clear the parent-death signal; re-arm it.
        if prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != reaper_pid:
            raise OSError("reaper is gone")
        os.chdir(cwd)

    try:
        candidate = subprocess.Popen(
            spec["command"], cwd=cwd, env=spec["env"], stdin=subprocess.DEVNULL,
            stdout=sys.stdout.fileno(), stderr=sys.stdout.fileno(), close_fds=True,
            # The reaper is single-threaded, so running Python between fork and
            # exec is safe here; the multi-threaded supervisor never does.
            start_new_session=True, umask=0o077,
            preexec_fn=drop_privileges,  # noqa: PLW1509
        )
    except (OSError, subprocess.SubprocessError) as exc:
        emit(event="launch_error", error=f"{type(exc).__name__}: {exc}")
        return 125
    import resource

    reported = False
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except ChildProcessError:
            break
        if pid == candidate.pid and not reported:
            # Popen must not reap it again; report the kernel wait status.
            candidate.returncode = os.waitstatus_to_exitcode(status)
            emit(event="exit", status=status)
            reported = True
    if not reported:
        emit(event="launch_error", error="candidate exit status was lost")
        return 125
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    emit(event="done", children_maxrss_kib=int(usage.ru_maxrss))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reaper", nargs=3, metavar=("SPEC", "EVENT_FD", "SUPERVISOR_PID"))
    args = parser.parse_args(argv)
    if args.reaper:
        spec, event_fd, supervisor_pid = args.reaper
        return _reaper(Path(spec), int(event_fd), int(supervisor_pid))
    parser.error("--reaper is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
