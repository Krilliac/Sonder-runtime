"""Linux uid-separated supervisor for unattended self-mod checks (#517).

This is the Linux counterpart of ``scripts/selfmod_low_integrity.py`` and
returns the same result schema.  The supervisor stays root and owns the
evaluator truth.  Candidate code runs as a distinct, dedicated, unprivileged
uid/gid with no supplementary groups and ``no_new_privs``, in its own session
and a fresh network namespace that holds only a loopback interface left down,
under a seccomp filter that confines socket creation to the namespace-scoped
address families, below per-process rlimits, with a scrubbed environment and
a private HOME/TMPDIR.  Protected evaluator truth must not be writable by that uid: the
supervisor proves this before launch (failing closed otherwise) and re-digests
it after the candidate is gone.

Process topology::

    supervisor (root, this process, host network namespace)
      '-- reaper (root, ``--reaper``; child subreaper, dies with supervisor;
            |     own network namespace, ``no_new_privs``)
            '-- candidate (candidate uid, new session) and every descendant

The reaper sets ``no_new_privs`` on itself, enters a new network namespace and
installs the socket filter before it launches anything, reports that
boundary, and then waits.  The supervisor confirms the boundary itself from
``/proc/<reaper>`` (a namespace inode distinct from its own, no interface but
``lo``, no IPv4 route and no IPv6 address, ``NoNewPrivs: 1``, one seccomp
filter more than its own) and only then lets the reaper launch the candidate.
The candidate inherits all of it and cannot leave it: joining the host
namespace needs ``CAP_SYS_ADMIN`` over it, and neither ``no_new_privs`` nor a
seccomp filter can be removed.  If any part cannot be established the run
fails closed; it never falls back to the host network.

The network namespace does not scope every address family: ``AF_VSOCK``
reaches the hypervisor host of a VM guest from any namespace, and others
(Bluetooth, CAN, TIPC, ...) are not namespace-aware either.  The filter
therefore allows ``socket``/``socketpair`` only for ``AF_UNIX``, ``AF_INET``,
``AF_INET6`` and ``AF_NETLINK`` (all namespace-scoped), fails every other
family with ``EAFNOSUPPORT``, fails the io_uring syscalls (whose socket
opcode would bypass the ``socket`` check) with ``ENOSYS``, and kills a process
that enters the kernel through a foreign syscall ABI (i386/x32/arm32), whose
socket calls the filter could not inspect.

The reaper exists so that orphaned candidate descendants are reaped instead of
lingering as zombies that count against the candidate uid's RLIMIT_NPROC.  The
supervisor tears the candidate down by uid, not by process group: every live
process whose real uid is the dedicated candidate uid belongs to the candidate
(the supervisor refuses to launch while any exist), so a grandchild that
called ``setsid()`` cannot escape.

What this boundary does NOT provide (see
``docs/architecture/REMAINING-SELFMOD-517-LINUX-ISOLATION.md``): the candidate
still produces the output the parent grades, so result independence comes
from the evaluator-held oracle (``scripts/selfmod_oracle.py``), which uses
this boundary and ``require_not_candidate_readable`` to keep held expected
values from the candidate uid; confidentiality of world-readable files is not
provided, job memory is enforced by sampling rather than a cgroup, and the
seccomp filter narrows socket families only, not the rest of the kernel
surface the candidate can reach.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import select
import shutil
import signal
import socket
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

# The network boundary the supervisor attests (see
# ``candidate_isolation.NETWORK_NAMESPACE``): a namespace distinct from the
# supervisor's whose only interface is a loopback left down, so the candidate
# has no IP route at all, not even to 127.0.0.1.  Address families the
# namespace does not scope are closed by the socket filter below.
NETWORK_ISOLATION = "netns"
_ISOLATED_INTERFACES = ["lo"]
# Fallback tunnel devices the kernel adds to every new network namespace
# while ``net.core.fb_tunnels_only_for_init_net`` is 0 (the default) and the
# matching module is loaded.  They are refused like any other interface; the
# refusal names the sysctl that stops the kernel creating them.
_FALLBACK_TUNNELS = frozenset({
    "erspan0", "gre0", "gretap0", "ip6_vti0", "ip6gre0", "ip6tnl0", "ip_vti0",
    "sit0", "tunl0",
})
_FB_TUNNELS_SYSCTL = Path("/proc/sys/net/core/fb_tunnels_only_for_init_net")
FALLBACK_TUNNEL_GUIDANCE = (
    "the kernel creates fallback tunnel devices in every new network namespace; "
    "set the sysctl net.core.fb_tunnels_only_for_init_net=1 on this host "
    f"(see {ISOLATION_DOC})"
)
_BOUNDARY_SECONDS = 10.0
# ioctl(2) request and interface flag (linux/sockios.h, linux/if.h).
_SIOCGIFFLAGS = 0x8913
_IFF_UP = 0x1

# prctl(2) options (linux/prctl.h); stable kernel ABI.
_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_NO_NEW_PRIVS = 38
_PR_GET_NO_NEW_PRIVS = 39
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2

# The socket filter the supervisor attests (see
# ``candidate_isolation.SOCKET_FILTER``).  Only these families, each scoped by
# the candidate's network namespace, may be created: AF_UNIX, AF_INET,
# AF_INET6, AF_NETLINK.  Every other family (AF_VSOCK above all) fails with
# EAFNOSUPPORT, and the io_uring syscalls fail with ENOSYS.
SOCKET_FILTER = "seccomp"
_ALLOWED_SOCKET_FAMILIES = (1, 2, 10, 16)
_IO_URING_SYSCALLS = (425, 426, 427)  # setup, enter, register: one table on all three ABIs
# Native syscall ABI per machine: (AUDIT_ARCH_*, socket, socketpair, x32).
# ``x32`` marks x86_64, whose kernel also accepts x32 numbers (bit 30 set)
# under the same audit arch; the filter kills those rather than map them.
_SECCOMP_ABIS = {
    "x86_64": (0xC000003E, 41, 53, True),
    "aarch64": (0xC00000B7, 198, 199, False),
    "riscv64": (0xC00000F3, 198, 199, False),
}
_X32_SYSCALL_BIT = 0x40000000
# Classic BPF opcodes and seccomp return actions (linux/filter.h, linux/seccomp.h).
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
# struct seccomp_data offsets: nr, arch, then args[0] (low word, little endian).
_SECCOMP_NR = 0
_SECCOMP_ARCH = 4
_SECCOMP_ARG0 = 16

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


def require_not_candidate_readable(paths: Sequence[str | os.PathLike[str]]) -> None:
    """Refuse when the configured candidate uid could read any of ``paths``.

    This is the confidentiality counterpart of
    ``require_not_candidate_writable`` for evaluator secrets such as the
    independent oracle's held expected values.  Each path must be a regular
    file (not a symlink) that the candidate credentials cannot read, inside
    a directory the candidate can neither list nor traverse; neither may
    carry a POSIX ACL.  Candidate ownership of either is refused outright,
    since an owner can always chmod its way back in.  Raises
    ``ProtectedPathExposed``.
    """
    _require_host()
    uid, gid = candidate_identity(None, None)
    for raw in paths:
        path = Path(os.path.abspath(os.fspath(raw)))
        for entry, expect_dir in ((path.parent, True), (path, False)):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                raise ProtectedPathExposed(f"confidential path is missing: {entry}") from None
            if stat.S_ISLNK(info.st_mode):
                raise ProtectedPathExposed(f"confidential path is a symlink: {entry}")
            if stat.S_ISDIR(info.st_mode) is not expect_dir or not (
                    expect_dir or stat.S_ISREG(info.st_mode)):
                raise ProtectedPathExposed(f"confidential path has the wrong type: {entry}")
            if _has_acl(entry):
                raise ProtectedPathExposed(f"confidential path carries a POSIX ACL: {entry}")
            if info.st_uid == uid:
                raise ProtectedPathExposed(f"candidate uid owns confidential path: {entry}")
            bits = (stat.S_IRGRP | stat.S_IXGRP, stat.S_IROTH | stat.S_IXOTH) if expect_dir \
                else (stat.S_IRGRP, stat.S_IROTH)
            if info.st_mode & (bits[0] if info.st_gid == gid else bits[1]):
                raise ProtectedPathExposed(f"confidential path is candidate-readable: {entry}")


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
    if not hasattr(os, "unshare") or not os.path.exists("/proc/self/ns/net"):
        raise LinuxIsolationUnavailable("kernel or Python lacks network namespaces")
    if "Seccomp" not in _proc_status(os.getpid()):
        raise LinuxIsolationUnavailable("kernel lacks seccomp filtering")
    if _seccomp_abi() is None:
        raise LinuxIsolationUnavailable(
            f"no candidate socket filter exists for the {platform.machine()!r} syscall ABI"
        )
    if _fallback_tunnels_expected():
        raise LinuxIsolationUnavailable(FALLBACK_TUNNEL_GUIDANCE)


def _fallback_tunnels_expected() -> bool:
    """True when a new network namespace will receive fallback tunnel devices.

    With the sysctl at 0, the kernel creates each loaded tunnel module's
    fallback device in every namespace, this one included, so seeing one here
    means the candidate's namespace would hold it too.
    """
    try:
        setting = _FB_TUNNELS_SYSCTL.read_text(encoding="utf-8").strip()
    except OSError:
        # Kernels without the sysctl (before 5.7) always create them; the
        # namespace check in the reaper still refuses such an interface.
        setting = "0"
    if setting != "0":
        return False
    return any(name in _FALLBACK_TUNNELS for _index, name in socket.if_nameindex())


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
    go_read, go_write = os.pipe()
    reaper = None
    drain = None
    try:
        os.set_blocking(read_fd, False)
        # ``-I`` keeps the candidate checkout (the reaper's eventual child
        # cwd) and user site-packages from shadowing this trusted file.
        reaper = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--reaper",
             str(spec_path), str(write_fd), str(os.getpid()), str(go_read)],
            cwd=str(control), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, pass_fds=(write_fd, go_read), start_new_session=True,
            env={"PATH": _DEFAULT_PATH},
        )
        os.close(write_fd)
        write_fd = -1
        os.close(go_read)
        go_read = -1
        assert reaper.stdout is not None
        drain = _Drain(reaper.stdout)
        events: list[dict[str, Any]] = []
        buffer = bytearray()
        # Nothing runs as the candidate until the boundary is confirmed here.
        # Any raise leaves ``go_write`` closed unwritten, and the reaper then
        # exits without launching.
        network = _confirm_boundary(reaper.pid, _await_boundary(read_fd, buffer, events, reaper))
        socket_filter = _socket_filter_report()
        os.write(go_write, b"1")
        os.close(go_write)
        go_write = -1
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
            "network": network, "no_new_privs": True, "socket_filter": socket_filter,
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
        for fd in (write_fd, go_read, go_write):
            if fd >= 0:
                os.close(fd)
        os.close(read_fd)
        if reaper is not None and reaper.poll() is None:
            reaper.kill()
            reaper.wait(timeout=5)
        if drain is not None:
            drain.join(5)


def _netns_inode(pid: int | str) -> int:
    """The inode naming the network namespace of ``pid`` (``"self"`` allowed)."""
    return os.stat(f"/proc/{pid}/ns/net").st_ino


def _proc_interfaces(pid: int | str) -> list[str]:
    """Interface names in the network namespace of ``pid``, from its /proc view."""
    lines = Path(f"/proc/{pid}/net/dev").read_text(encoding="utf-8").splitlines()
    # Two header lines, then ``name: counters`` per interface.
    return sorted(line.split(":", 1)[0].strip() for line in lines[2:] if ":" in line)


def _proc_has_route(pid: int | str) -> bool:
    """Whether the network namespace of ``pid`` has any IPv4 route or IPv6 address.

    A fresh namespace has neither while ``lo`` is down: bringing ``lo`` up
    adds the 127.0.0.0/8 local routes to ``fib_trie`` and ``::1`` to
    ``if_inet6``.  ``if_inet6`` is absent on a kernel without IPv6.
    """
    if Path(f"/proc/{pid}/net/fib_trie").read_text(encoding="utf-8").strip():
        return True
    try:
        return bool(Path(f"/proc/{pid}/net/if_inet6").read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return False


def _seccomp_filters(pid: int | str) -> tuple[str, int | None]:
    """``Seccomp`` mode and ``Seccomp_filters`` count (``None`` before 5.9) of ``pid``."""
    fields = _proc_status(pid) if isinstance(pid, int) else _proc_status(os.getpid())
    count = fields.get("Seccomp_filters")
    return fields.get("Seccomp", ""), int(count) if count and count.isdigit() else None


def _socket_filter_report() -> dict[str, object]:
    """The socket filter this module installs, as the attestation records it."""
    return {"mechanism": SOCKET_FILTER,
            "socket_families": list(_ALLOWED_SOCKET_FAMILIES), "io_uring": "denied"}


def _confirm_boundary(reaper_pid: int, event: dict[str, Any]) -> dict[str, object]:
    """Confirm, from outside, the boundary the reaper says it entered.

    The reaper (still root, nothing launched yet) reported its network
    namespace and socket filter.  This process re-reads the facts from
    ``/proc/<reaper>`` instead of trusting the report: the namespace inode
    must differ from the supervisor's own and equal the reported one, the
    namespace may hold no interface but ``lo`` and no IPv4 route or IPv6
    address (so ``lo`` is down), ``NoNewPrivs`` must be set, and the reaper
    must be in seccomp filter mode with exactly one filter more than this
    process.  The candidate inherits all of it from the reaper.  Any mismatch
    raises ``LinuxIsolationUnavailable`` and the candidate is never launched.
    """
    try:
        supervisor_netns = _netns_inode("self")
        observed = _netns_inode(reaper_pid)
        interfaces = _proc_interfaces(reaper_pid)
        routed = _proc_has_route(reaper_pid)
    except OSError as exc:
        raise LinuxIsolationUnavailable(
            f"cannot observe the candidate network namespace: {type(exc).__name__}"
        ) from exc
    if observed == supervisor_netns:
        raise LinuxIsolationUnavailable("candidate parent shares the supervisor network namespace")
    if event.get("netns_inode") != observed:
        raise LinuxIsolationUnavailable("reported network namespace does not match the observed one")
    if interfaces != _ISOLATED_INTERFACES or event.get("interfaces") != _ISOLATED_INTERFACES:
        raise LinuxIsolationUnavailable(f"candidate network namespace has interfaces {interfaces!r}")
    if routed or event.get("loopback_up") is not False:
        raise LinuxIsolationUnavailable("candidate network namespace has loopback up")
    if _proc_status(reaper_pid).get("NoNewPrivs") != "1":
        raise LinuxIsolationUnavailable("candidate parent lacks no_new_privs")
    own_mode, own_filters = _seccomp_filters("self")
    mode, filters = _seccomp_filters(reaper_pid)
    if (mode != str(_SECCOMP_MODE_FILTER) or event.get("socket_filter") != _socket_filter_report()
            or (filters is not None and filters != (own_filters or 0) + 1)):
        raise LinuxIsolationUnavailable("candidate parent lacks the socket filter")
    return {
        "isolation": NETWORK_ISOLATION, "netns_inode": observed,
        "supervisor_netns_inode": supervisor_netns,
        "interfaces": list(_ISOLATED_INTERFACES), "loopback_up": False,
    }


def _await_boundary(
    read_fd: int, buffer: bytearray, events: list[dict[str, Any]],
    reaper: subprocess.Popen,
) -> dict[str, Any]:
    """Wait (bounded) for the reaper's boundary report; raise on any failure."""

    def reported() -> dict[str, Any] | None:
        return next((e for e in events if e.get("event") in {"boundary", "launch_error"}), None)

    deadline = time.monotonic() + _BOUNDARY_SECONDS
    open_pipe = True
    while open_pipe and reported() is None and time.monotonic() < deadline:
        ready, _w, _x = select.select([read_fd], [], [], _SAMPLE_INTERVAL_SECONDS)
        if ready:
            open_pipe = _read_events(read_fd, buffer, events)
    found = reported()
    if found is None:
        raise LinuxIsolationUnavailable("candidate boundary was not reported; nothing launched")
    if found.get("event") == "launch_error":
        raise LinuxIsolationUnavailable(
            f"candidate launch failed: {str(found.get('error', ''))[:300]}"
        )
    if reaper.poll() is not None:
        raise LinuxIsolationUnavailable("candidate parent exited before launch")
    return found


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


def _loopback_up() -> bool:
    import fcntl
    import struct

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        request = struct.pack("16sH22x", b"lo", 0)
        reply = fcntl.ioctl(probe.fileno(), _SIOCGIFFLAGS, request)
    return bool(struct.unpack("16sH22x", reply)[1] & _IFF_UP)


def _enter_network_namespace() -> dict[str, object]:
    """Move this (root, single-threaded) process into a fresh network namespace.

    The namespace is left as the kernel creates it: only ``lo``, and ``lo``
    down, so there is no route anywhere.  Raises ``OSError`` when the
    namespace cannot be created or does not look like that.
    """
    host = _netns_inode("self")
    os.unshare(os.CLONE_NEWNET)
    inode = _netns_inode("self")
    if inode == host:
        raise OSError("network namespace did not change")
    interfaces = sorted(name for _index, name in socket.if_nameindex())
    if interfaces != _ISOLATED_INTERFACES:
        guidance = ""
        if any(name in _FALLBACK_TUNNELS for name in interfaces):
            guidance = "; " + FALLBACK_TUNNEL_GUIDANCE
        raise OSError(f"new network namespace has interfaces {interfaces!r}{guidance}")
    if _loopback_up():
        raise OSError("new network namespace has loopback up")
    return {"netns_inode": inode, "interfaces": interfaces, "loopback_up": False}


def _seccomp_abi() -> tuple[int, int, int, bool] | None:
    return _SECCOMP_ABIS.get(platform.machine())


def _socket_filter_program() -> list[tuple[int, int, int, int]]:
    """Assemble the classic-BPF socket filter for this machine's syscall ABI.

    Returns ``(code, jt, jf, k)`` instructions.  Raises ``OSError`` on an ABI
    without a filter, so the caller fails closed instead of running
    unfiltered.
    """
    abi = _seccomp_abi()
    if abi is None:
        raise OSError(f"no socket filter for the {platform.machine()!r} syscall ABI")
    audit_arch, socket_nr, socketpair_nr, x32 = abi
    # Symbolic program: ("ld", offset) | ("jeq"/"jge", k, true, false) |
    # ("ret", action) | ("label", name).  A jump target of None is the next
    # instruction.
    source: list[tuple[Any, ...]] = [
        ("ld", _SECCOMP_ARCH),
        # A foreign ABI (i386 via int 0x80, arm32 compat) has other syscall
        # numbers and, for i386, socketcall(2) with arguments in memory.
        ("jeq", audit_arch, None, "kill"),
        ("ld", _SECCOMP_NR),
    ]
    if x32:
        source.append(("jge", _X32_SYSCALL_BIT, "kill", None))
    source += [
        ("jeq", socket_nr, "family", None),
        ("jeq", socketpair_nr, "family", None),
        *(("jeq", number, "nosys", None) for number in _IO_URING_SYSCALLS),
        ("ret", _SECCOMP_RET_ALLOW),
        ("label", "family"),
        ("ld", _SECCOMP_ARG0),
        *(("jeq", family, "allow", None) for family in _ALLOWED_SOCKET_FAMILIES),
        ("ret", _SECCOMP_RET_ERRNO | 97),  # EAFNOSUPPORT
        ("label", "allow"),
        ("ret", _SECCOMP_RET_ALLOW),
        ("label", "nosys"),
        ("ret", _SECCOMP_RET_ERRNO | 38),  # ENOSYS
        ("label", "kill"),
        ("ret", _SECCOMP_RET_KILL_PROCESS),
    ]
    labels: dict[str, int] = {}
    body: list[tuple[Any, ...]] = []
    for item in source:
        if item[0] == "label":
            labels[item[1]] = len(body)
        else:
            body.append(item)

    def offset(index: int, target: str | None) -> int:
        if target is None:
            return 0
        distance = labels[target] - index - 1
        if not 0 <= distance <= 255:
            raise OSError("socket filter jump out of range")
        return distance

    program = []
    for index, item in enumerate(body):
        if item[0] == "ld":
            program.append((_BPF_LD_W_ABS, 0, 0, item[1]))
        elif item[0] == "ret":
            program.append((_BPF_RET_K, 0, 0, item[1]))
        else:
            code = _BPF_JEQ_K if item[0] == "jeq" else _BPF_JGE_K
            program.append((code, offset(index, item[2]), offset(index, item[3]), item[1]))
    return program


def _install_socket_filter() -> dict[str, object]:
    """Install the socket filter on this (single-threaded) process and its future children.

    Needs ``no_new_privs`` or ``CAP_SYS_ADMIN``.  Raises ``OSError`` when the
    filter cannot be installed; there is no unfiltered fallback.
    """
    import ctypes

    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]

    program = _socket_filter_program()
    instructions = (SockFilter * len(program))(*(SockFilter(*item) for item in program))
    fprog = SockFprog(len(program), instructions)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"cannot install the socket filter: {os.strerror(errno)}")
    if _seccomp_filters("self")[0] != str(_SECCOMP_MODE_FILTER):
        raise OSError("socket filter is not in force")
    return _socket_filter_report()


def _reaper(spec_path: Path, event_fd: int, supervisor_pid: int, go_fd: int) -> int:
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
    # The boundary every candidate descendant inherits, entered here while
    # root and before anything is launched.  Failure is fatal; there is no
    # fallback to the host network.
    if prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 or prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        emit(event="launch_error", error="no_new_privs unavailable")
        return 125
    try:
        boundary = _enter_network_namespace()
    except OSError as exc:
        emit(event="launch_error", error=f"network namespace unavailable: {type(exc).__name__}: {exc}")
        return 125
    try:
        boundary["socket_filter"] = _install_socket_filter()
    except OSError as exc:
        emit(event="launch_error", error=f"socket filter unavailable: {type(exc).__name__}: {exc}")
        return 125
    emit(event="boundary", **boundary)
    with os.fdopen(go_fd, "rb", buffering=0) as go:
        if go.read(1) != b"1":
            # The supervisor did not confirm the boundary; launch nothing.
            return 125
    isolated_netns = int(boundary["netns_inode"])
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
        if _netns_inode("self") != isolated_netns:
            raise OSError("candidate is outside the confirmed network namespace")
        if _seccomp_filters("self")[0] != str(_SECCOMP_MODE_FILTER):
            raise OSError("candidate is outside the socket filter")
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
    parser.add_argument("--reaper", nargs=4,
                        metavar=("SPEC", "EVENT_FD", "SUPERVISOR_PID", "GO_FD"))
    args = parser.parse_args(argv)
    if args.reaper:
        spec, event_fd, supervisor_pid, go_fd = args.reaper
        return _reaper(Path(spec), int(event_fd), int(supervisor_pid), int(go_fd))
    parser.error("--reaper is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
