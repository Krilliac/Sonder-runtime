"""Windows AppContainer sandbox for the headless Blender that runs model-written scripts.

This module is the security boundary of ``mesh_generate``.  The AST gate and the restricted
builtins in the harness are cheap first filters that catch mistakes; they are not what stops a
hostile script.  What stops it is that Blender runs as a zero-capability AppContainer process on
its own desktop inside a Job Object.  Each point below is exercised by a real-Blender
regression test in ``tests/test_meshgen_integration.py``, with an unsandboxed control showing the
probe really would succeed without the sandbox:

* files: only its per-build run directory is writable and only that plus the Blender install
  directory are readable.  User files, the repository and credential stores are unreadable;
  writes elsewhere, symlink/hardlink/junction tricks and ``OBJ`` imports of outside files fail;
* network: no network capability, so loopback, external TCP and DNS all fail;
* processes: ``ActiveProcessLimit=1`` stops a second process; the job memory limit and
  ``KILL_ON_JOB_CLOSE`` bound and reap the build;
* screen: the process runs on a dedicated, otherwise empty desktop;
* state: every build starts from wiped AppContainer storage and a slot profile no concurrent
  build shares.

KNOWN GAP, measured: the user's HKCU registry stays READABLE (environment variables, recent
documents, application settings), because those keys grant read to ``ALL APPLICATION PACKAGES``
and every AppContainer is in that group.  Opting out (LPAC) was tried and does not work for this
workload: an LPAC Blender dies at start (SxS manifest activation needs ``registryRead``, whose SID
is also in the user's HKCU ACLs) and, even with that capability, cannot create an OpenGL context
(the GPU driver is not reachable from LPAC), so rendering crashes.  ``registry_isolation``
measures the gap and ``preflight`` reports it in the tool's result.  A DENY ACE for the slot's
package SID on the registry key does NOT help (tested on a scratch key: the AppContainer access
check still reads it).  Closing it needs a different identity (a separate low-privilege Windows
account or VM with an empty HKCU).  Model-written code never runs in the sandbox (it is recorded as data on the host), so the
gap is only reachable through the trusted replay and kit code.  The host bounds every string that flows back
from the build.

Not proven or not covered: disk consumption inside the run directory during a build is not
bounded (only checked afterwards); CPU use is bounded only by the timeout; GetAsyncKeyState and
cross-sandbox ``OpenProcess`` are probed in the tests but not treated as proven; a Windows
AppContainer/kernel escape is out of scope.

Footprint on the machine (documented in ``docs/runbooks/mesh-generation.md``):

* up to ``SLOT_COUNT`` AppContainer profiles ``Sonder.MeshGen.s0`` ... (per user, created on
  first use).  A build holds one slot (cross-process named mutex), so concurrent builds are
  isolated from each other (separate SIDs) and the slot's storage is wiped before each build;
* per slot ONE persistent ACL entry: ``(OI)(CI)(RX)`` for that profile's SID on the Blender
  install directory, added only when missing.  Undo with ``icacls <blender dir> /remove:g *<SID>``;
* per build, a fresh run directory with a ``(OI)(CI)(M)`` grant that is removed (and the
  directory deleted) when the build ends.  No ACL is ever placed on the kit's source directory,
  the repository or any user directory, and the Blender directory is refused if it is, contains
  or sits inside the user's home or contains the kit.

Pure helpers (environment, grant planning, mode selection) are separate from the ctypes launch
so they can be unit tested anywhere.  Thread affinity: a build acquires and releases its slot
mutex on one thread; every call is otherwise self-contained.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

PROFILE_PREFIX = "Sonder.MeshGen.s"
SLOT_COUNT = 3
SLOT_WAIT_SECONDS = 180
OPT_OUT_ENV = "SONDER_MESHGEN_UNSANDBOXED"
MEMORY_ENV = "SONDER_MESHGEN_MEM_MB"
DEFAULT_MEMORY_MB = 4096
MIN_MEMORY_MB = 256
MAX_MEMORY_MB = 65536
OUTPUT_TAIL_BYTES = 64 * 1024
MAX_TREE_BYTES = 1024 * 1024 * 1024
SANDBOXED = "appcontainer"
UNSANDBOXED = "unsandboxed"


class SandboxUnavailable(RuntimeError):
    """The sandbox cannot be set up; callers must fail closed (never run unsandboxed silently)."""


# --------------------------------------------------------------------------- pure logic

def memory_limit_mb(env) -> int:
    """Job-wide memory limit in MB from ``SONDER_MESHGEN_MEM_MB`` (clamped), default 4096."""
    try:
        value = int(str(env.get(MEMORY_ENV, "") or "").strip() or DEFAULT_MEMORY_MB)
    except ValueError:
        value = DEFAULT_MEMORY_MB
    return max(MIN_MEMORY_MB, min(value, MAX_MEMORY_MB))


def opt_out_requested(env) -> bool:
    return str(env.get(OPT_OUT_ENV, "") or "").strip().lower() in ("1", "true", "yes", "on")


def decide_mode(env, os_name: str) -> str:
    """``appcontainer`` on Windows; ``unsandboxed`` only with the explicit opt-out, on any platform.

    There is no sandbox implementation off Windows, so there the opt-out is mandatory too:
    without it the call raises ``SandboxUnavailable`` (fail closed everywhere).
    """
    if opt_out_requested(env):
        return UNSANDBOXED
    if os_name != "nt":
        raise SandboxUnavailable("no sandbox exists on this platform; set %s=1 to run unsandboxed "
                                 "(model-written code then has this server's authority)" % OPT_OUT_ENV)
    return SANDBOXED


def sandbox_env(run_dir, blender_dir, system_root: str) -> dict:
    """The complete environment of the sandboxed process: nothing is inherited from the server."""
    run = Path(run_dir)
    home = run / "home"
    tmp = run / "tmp"
    root = system_root or r"C:\Windows"
    return {
        "SYSTEMROOT": root, "WINDIR": root,
        "PATH": os.pathsep.join([str(blender_dir), root + r"\System32", root]),
        "TEMP": str(tmp), "TMP": str(tmp),
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "BLENDER_USER_CONFIG": str(run / "blender" / "config"),
        "BLENDER_USER_SCRIPTS": str(run / "blender" / "scripts"),
        "BLENDER_USER_DATAFILES": str(run / "blender" / "datafiles"),
        "BLENDER_USER_EXTENSIONS": str(run / "blender" / "extensions"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


def environment_directories(run_dir) -> list[Path]:
    """Directories the environment points at; they must exist before launch."""
    run = Path(run_dir)
    return [run / "tmp", run / "home" / "AppData" / "Roaming", run / "home" / "AppData" / "Local",
            run / "blender" / "config", run / "blender" / "scripts", run / "blender" / "datafiles",
            run / "blender" / "extensions", run / "out"]


@dataclass(frozen=True)
class Grant:
    path: str
    permission: str   # icacls permission letters, e.g. "M" or "RX"
    persistent: bool  # True only for the one-time Blender install grant


def _norm(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _overlaps(a: str, b: str) -> bool:
    """True when one path is the same as, inside, or a parent of the other."""
    try:
        common = os.path.commonpath([a, b])
    except ValueError:
        return False
    return common in (a, b)


def _covers(parent: str, child: str) -> bool:
    """True when ``parent`` is ``child`` or one of its ancestors."""
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def plan_grants(run_dir, blender_dir, kit_source_dir=None, protected=()) -> list[Grant]:
    """The ACL entries a build needs: Modify on the run dir, Read+Execute on the Blender dir.

    Refuses a run directory that is, contains, or sits inside the kit's source directory, and one
    that is or contains any ``protected`` directory (home, repository roots...), so a grant can
    never expose them.
    """
    run = _norm(run_dir)
    if kit_source_dir and _overlaps(run, _norm(kit_source_dir)):
        raise SandboxUnavailable("run directory %s overlaps the kit source directory (%s); refusing to grant access"
                                 % (run, kit_source_dir))
    for other in protected:
        # A grant is inherited downwards, so only a run dir that IS or CONTAINS a protected
        # directory (home, a repository root) would expose it; sitting below one is fine.
        if _covers(run, _norm(other)):
            raise SandboxUnavailable("run directory %s would cover the protected directory %s" % (run, other))
    return [Grant(str(run_dir), "M", False), Grant(str(blender_dir), "RX", True)]


def check_blender_dir(blender_dir, kit_source_dir=None, protected=()) -> str:
    """Validate the directory that receives the persistent read grant; returns its resolved path.

    Symlinks and shims are resolved first.  The directory must not be, contain, or sit inside any
    protected directory (the user's home and its ancestors/descendants), must not contain the
    kit's source directory, and must not be a drive root.
    """
    real = _norm(blender_dir)
    if os.path.dirname(real) == real:
        raise SandboxUnavailable("refusing to grant read access to a drive root (%s)" % real)
    for other in protected:
        norm = _norm(other)
        if _overlaps(real, norm):
            raise SandboxUnavailable(
                "the Blender directory %s overlaps the protected directory %s; a read grant there would expose "
                "user data. Install Blender outside the user profile." % (real, other))
    if kit_source_dir and _covers(real, _norm(kit_source_dir)):
        raise SandboxUnavailable("the Blender directory %s contains the kit source directory; refusing to grant" % real)
    return real


def tree_size(path, cap: int = MAX_TREE_BYTES) -> int:
    """Total bytes of regular files under ``path``, stopping once ``cap`` is exceeded."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    if total > cap:
                        return total
        except OSError:
            continue
    return total


def wipe_directory_contents(root) -> list[str]:
    """Delete everything below the top-level directories of ``root``; returns problems.

    Links and junctions are removed as links (never followed).  Top-level directories stay so the
    profile keeps its structure.
    """
    problems: list[str] = []

    def is_link(path: str) -> bool:
        isjunction = getattr(os.path, "isjunction", lambda _p: False)
        return os.path.islink(path) or isjunction(path)

    def remove(path: str) -> None:
        try:
            if is_link(path):
                try:
                    os.unlink(path)
                except OSError:
                    os.rmdir(path)
            elif os.path.isdir(path):
                for name in os.listdir(path):
                    remove(os.path.join(path, name))
                os.rmdir(path)
            else:
                os.chmod(path, 0o600)
                os.remove(path)
        except OSError as exc:
            problems.append("%s: %s" % (path, exc.strerror or exc))

    if not os.path.isdir(root):
        return problems
    for top in os.listdir(root):
        top_path = os.path.join(root, top)
        if is_link(top_path) or not os.path.isdir(top_path):
            remove(top_path)
            continue
        for name in os.listdir(top_path):
            remove(os.path.join(top_path, name))
    return problems


def tail_bytes(path, limit: int = OUTPUT_TAIL_BYTES) -> bytes:
    """The last ``limit`` bytes of a file (all of it when smaller); empty when missing."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read(limit)
    except OSError:
        return b""


# --------------------------------------------------------------------------- Windows plumbing

def _icacls(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls"] + args, capture_output=True, text=True, timeout=60, check=False,
                          stdin=subprocess.DEVNULL)


_PROFILE_LOCK = threading.Lock()
_SLOTS: dict = {}
_GRANTED: set = set()


@dataclass
class Slot:
    index: int
    name: str
    sid_ptr: int      # PSID kept for the life of the process (a handful of bytes per slot)
    sid: str
    folder: str       # the profile's AppContainer storage root
    mutex: object = field(default=None, repr=False)


def _slot_profile(index: int) -> Slot:
    """Create the slot's profile (or derive its SID when it exists)."""
    from ctypes import wintypes as W

    name = "%s%d" % (PROFILE_PREFIX, index)
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    userenv.GetAppContainerFolderPath.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    userenv.GetAppContainerFolderPath.restype = ctypes.c_long
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    advapi.FreeSid.argtypes = [ctypes.c_void_p]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    psid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(name, "Sonder MeshGen %d" % index, "Sonder mesh_generate sandbox",
                                           None, 0, ctypes.byref(psid))
    if (hr & 0xFFFFFFFF) == 0x800700B7:  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        hr = userenv.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(psid))
    if hr != 0:
        raise SandboxUnavailable("AppContainer profile %s unavailable (hr=0x%08X)" % (name, hr & 0xFFFFFFFF))
    raw = ctypes.c_void_p()
    if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(raw)):
        advapi.FreeSid(psid)
        raise SandboxUnavailable("could not read the AppContainer SID")
    try:
        sid = ctypes.wstring_at(raw.value)
    finally:
        k32.LocalFree(raw)
    path = ctypes.c_void_p()
    hr = userenv.GetAppContainerFolderPath(sid, ctypes.byref(path))
    if hr != 0:
        advapi.FreeSid(psid)
        raise SandboxUnavailable("AppContainer storage folder unavailable (hr=0x%08X)" % (hr & 0xFFFFFFFF))
    try:
        folder = ctypes.wstring_at(path.value)
    finally:
        ole.CoTaskMemFree(path)
    return Slot(index, name, int(psid.value), sid, folder)


def slot_profile(index: int) -> Slot:
    with _PROFILE_LOCK:
        if index not in _SLOTS:
            _SLOTS[index] = _slot_profile(index)
        return _SLOTS[index]


def ensure_blender_access(blender_dir, sid: str, kit_source_dir=None, protected=()) -> bool:
    """Grant the container Read+Execute on the Blender install dir if (and only if) it is missing.

    Validated first (``check_blender_dir``), idempotent, and done once per directory and SID per
    process.  Returns True when a grant was added.
    """
    real = check_blender_dir(blender_dir, kit_source_dir, protected)
    key = (sid, real)
    with _PROFILE_LOCK:
        if key in _GRANTED:
            return False
        shown = _icacls([real])
        if shown.returncode == 0 and sid in shown.stdout:
            _GRANTED.add(key)
            return False
        done = _icacls([real, "/grant", "*%s:(OI)(CI)(RX)" % sid])
        if done.returncode != 0:
            raise SandboxUnavailable(
                "cannot grant the sandbox read access to %s (%s). Run once as an account that can edit its "
                "ACL: icacls \"%s\" /grant *%s:(OI)(CI)(RX)"
                % (real, (done.stderr or done.stdout).strip()[:200], real, sid))
        _GRANTED.add(key)
        return True


def prepare(blender_dir, kit_source_dir=None, protected=None) -> str:
    """Make sure slot 0's profile and its Blender grant exist; returns its SID. Fails closed."""
    if os.name != "nt":
        raise SandboxUnavailable("the AppContainer sandbox exists only on Windows")
    protected = [Path.home()] if protected is None else protected
    try:
        slot = slot_profile(0)
        ensure_blender_access(blender_dir, slot.sid, kit_source_dir, protected)
    except SandboxUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any setup failure must surface as "unavailable"
        raise SandboxUnavailable("sandbox setup failed: %s: %s" % (type(exc).__name__, exc)) from exc
    return slot.sid


_ISOLATION_CACHE: dict = {}


def registry_isolation(blender_dir, refresh: bool = False) -> str:
    """``isolated`` when the sandbox cannot open ``HKCU\\Environment``, ``readable`` when it can.

    Measured, not assumed: a throw-away sandboxed ``reg.exe query`` (default value only, so nothing
    sensitive is ever printed) runs once per process and the result is cached.
    """
    if not refresh and "state" in _ISOLATION_CACHE:
        return _ISOLATION_CACHE["state"]
    import tempfile

    base = Path(tempfile.mkdtemp(prefix="sonder-meshgen-probe-"))
    state = "readable"
    try:
        with granted_run_dir(base, blender_dir) as build:
            root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
            outcome = run_sandboxed([root + r"\System32\reg.exe", "query", "HKCU\\Environment", "/ve"],
                                    cwd=build.run, env=sandbox_env(build.run, blender_dir, root), timeout=30,
                                    slot=build.slot, memory_mb=256)
            state = "readable" if outcome.returncode == 0 else "isolated"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    _ISOLATION_CACHE["state"] = state
    return state


def preflight(blender_dir) -> dict:
    """Set up slot 0, then measure registry isolation; returns report fields. Fails closed.

    HKCU readability is reported (``registry``/``warning``); it cannot be closed from here (see the
    module docstring), so it does not fail the preflight.
    """
    prepare(blender_dir)
    state = registry_isolation(blender_dir)
    info = {"registry": state}
    if state != "isolated":
        info["warning"] = ("HKCU is readable by the Blender process; no model-written code runs in it "
                           "(see the mesh-generation runbook)")
    return info


@contextlib.contextmanager
def acquire_slot(blender_dir, kit_source_dir=None, protected=None, wait: float = SLOT_WAIT_SECONDS):
    """Hold one slot (profile) for the duration of a build; blocks while all slots are busy.

    The slot is guarded by a named mutex, so it also excludes other server processes.  Entry
    validates and, if needed, grants the Blender directory for that slot's SID and wipes the
    slot's AppContainer storage.
    """
    from ctypes import wintypes as W

    if os.name != "nt":
        raise SandboxUnavailable("the AppContainer sandbox exists only on Windows")
    protected = [Path.home()] if protected is None else protected
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, W.BOOL, W.LPCWSTR]
    k32.CreateMutexW.restype = W.HANDLE
    k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k32.WaitForSingleObject.restype = W.DWORD
    k32.ReleaseMutex.argtypes = [W.HANDLE]
    k32.CloseHandle.argtypes = [W.HANDLE]
    handles = []
    for index in range(SLOT_COUNT):
        handle = k32.CreateMutexW(None, False, "Local\\SonderMeshGenSlot%d" % index)
        if not handle:
            for opened in handles:
                k32.CloseHandle(opened)
            raise SandboxUnavailable("cannot create the sandbox slot mutex (winerror %d)" % ctypes.get_last_error())
        handles.append(handle)
    chosen = None
    deadline = time.monotonic() + wait
    try:
        while chosen is None:
            for index, handle in enumerate(handles):
                status = k32.WaitForSingleObject(handle, 0)
                if status in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
                    chosen = index
                    break
                if status == 0xFFFFFFFF:
                    raise SandboxUnavailable("waiting for a sandbox slot failed (winerror %d)" % ctypes.get_last_error())
            if chosen is None:
                if time.monotonic() > deadline:
                    raise SandboxUnavailable("all %d sandbox slots stayed busy for %ds" % (SLOT_COUNT, wait))
                time.sleep(0.2)
        try:
            slot = slot_profile(chosen)
            ensure_blender_access(blender_dir, slot.sid, kit_source_dir, protected)
            problems = wipe_directory_contents(slot.folder)  # the AC storage root (holds Temp, INetCache, ...)
            if problems:
                raise SandboxUnavailable("cannot reset the sandbox storage of %s: %s" % (slot.name, "; ".join(problems[:3])))
            yield slot
        finally:
            k32.ReleaseMutex(handles[chosen])
    finally:
        for opened in handles:
            k32.CloseHandle(opened)


@dataclass
class Build:
    run: Path
    slot: Slot
    problems: list = field(default_factory=list)  # cleanup failures, filled in when the block exits


@contextlib.contextmanager
def granted_run_dir(base, blender_dir, kit_source_dir=None, protected=None):
    """A fresh run directory under ``base`` with a Modify grant for this build only.

    Holds a slot for the whole build.  The grant is removed and the directory deleted on exit,
    success or failure; cleanup failures are recorded in ``Build.problems`` instead of vanishing.
    """
    protected = [Path.home()] if protected is None else protected
    with acquire_slot(blender_dir, kit_source_dir, protected) as slot:
        run = Path(base) / (".run-" + uuid.uuid4().hex[:12])
        plan = plan_grants(run, blender_dir, kit_source_dir, protected)
        run.mkdir(parents=True, exist_ok=False)
        build = Build(run, slot)
        try:
            done = _icacls([str(run), "/grant", "*%s:(OI)(CI)(%s)" % (slot.sid, plan[0].permission)])
            if done.returncode != 0:
                raise SandboxUnavailable("cannot grant the sandbox access to its run directory: %s"
                                         % (done.stderr or done.stdout).strip()[:200])
            for directory in environment_directories(run):
                directory.mkdir(parents=True, exist_ok=True)
            yield build
        finally:
            removed = _icacls([str(run), "/remove:g", "*%s" % slot.sid])
            if removed.returncode != 0:
                build.problems.append("could not remove the run-directory grant: %s"
                                      % (removed.stderr or removed.stdout).strip()[:200])
            shutil.rmtree(run, ignore_errors=True)
            if run.exists():
                build.problems.append("could not delete the run directory %s" % run)


@dataclass
class SandboxRun:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    memory_exceeded: bool   # the job's peak reached its limit; the caller decides if that explains a failure
    peak_job_mb: int
    memory_limit_mb: int


def run_sandboxed(argv, *, cwd, env, timeout, slot: Slot, memory_mb: int) -> SandboxRun:
    """Launch ``argv`` suspended as a zero-capability AppContainer process on its own desktop, in a limited Job.

    The process is resumed only after ``AssignProcessToJobObject`` succeeded; if that or any
    setup step fails the process is terminated and ``SandboxUnavailable`` is raised (fail closed).
    """
    from ctypes import wintypes as W

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class SecurityCapabilities(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.c_void_p),
                    ("CapabilityCount", W.DWORD), ("Reserved", W.DWORD)]

    class StartupInfo(ctypes.Structure):
        _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                    ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                    ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                    ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE),
                    ("hStdError", W.HANDLE)]

    class StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD),
                    ("dwThreadId", W.DWORD)]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", W.BOOL)]

    class JobBasic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class JobExtended(ctypes.Structure):
        _fields_ = [("Basic", JobBasic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class Trustee(ctypes.Structure):
        _fields_ = [("pMultipleTrustee", ctypes.c_void_p), ("MultipleTrusteeOperation", ctypes.c_int),
                    ("TrusteeForm", ctypes.c_int), ("TrusteeType", ctypes.c_int), ("ptstrName", ctypes.c_void_p)]

    class ExplicitAccess(ctypes.Structure):
        _fields_ = [("grfAccessPermissions", W.DWORD), ("grfAccessMode", ctypes.c_int),
                    ("grfInheritance", W.DWORD), ("Trustee", Trustee)]

    k32.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL, W.DWORD,
                                   ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(StartupInfoEx),
                                   ctypes.POINTER(ProcessInformation)]
    k32.CreateProcessW.restype = W.BOOL
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD,
                                                      ctypes.POINTER(ctypes.c_size_t)]
    k32.InitializeProcThreadAttributeList.restype = W.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, W.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k32.UpdateProcThreadAttribute.restype = W.BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]
    k32.CreateJobObjectW.restype = W.HANDLE
    k32.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]
    k32.SetInformationJobObject.restype = W.BOOL
    k32.QueryInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD,
                                              ctypes.POINTER(W.DWORD)]
    k32.QueryInformationJobObject.restype = W.BOOL
    k32.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    k32.AssignProcessToJobObject.restype = W.BOOL
    k32.ResumeThread.argtypes = [W.HANDLE]
    k32.ResumeThread.restype = W.DWORD
    k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k32.WaitForSingleObject.restype = W.DWORD
    k32.GetExitCodeProcess.argtypes = [W.HANDLE, ctypes.POINTER(W.DWORD)]
    k32.GetExitCodeProcess.restype = W.BOOL
    k32.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
    k32.TerminateProcess.restype = W.BOOL
    k32.CloseHandle.argtypes = [W.HANDLE]
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    user32.CreateDesktopW.argtypes = [W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p]
    user32.CreateDesktopW.restype = W.HANDLE
    user32.CloseDesktop.argtypes = [W.HANDLE]
    user32.GetProcessWindowStation.restype = W.HANDLE
    user32.GetUserObjectInformationW.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
    user32.GetUserObjectInformationW.restype = W.BOOL
    advapi.GetSecurityInfo.argtypes = [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetSecurityInfo.restype = W.DWORD
    advapi.SetEntriesInAclW.argtypes = [W.ULONG, ctypes.POINTER(ExplicitAccess), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
    advapi.SetEntriesInAclW.restype = W.DWORD
    advapi.SetSecurityInfo.argtypes = [W.HANDLE, ctypes.c_int, W.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p]
    advapi.SetSecurityInfo.restype = W.DWORD

    invalid = ctypes.c_void_p(-1).value
    opened: list = []

    def open_inheritable(path, write: bool):
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, True)
        access, disposition = (0x40000000 | 0x00100000, 2) if write else (0x80000000, 3)
        handle = k32.CreateFileW(path, access, 3, ctypes.byref(attrs), disposition, 0x80, None)
        if handle in (None, invalid):
            raise SandboxUnavailable("cannot open %s: winerror %d" % (path, ctypes.get_last_error()))
        opened.append(handle)
        return handle

    def grant_slot(handle, what: str, mask: int) -> None:
        """Add an ACE for this slot's SID to a window station or desktop (SE_WINDOW_OBJECT)."""
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        new_dacl = ctypes.c_void_p()
        try:
            if advapi.GetSecurityInfo(handle, 7, 4, None, None, ctypes.byref(dacl), None,
                                      ctypes.byref(descriptor)) != 0:
                raise SandboxUnavailable("cannot read the %s DACL" % what)
            access = ExplicitAccess()
            access.grfAccessPermissions = mask
            access.grfAccessMode = 1  # GRANT_ACCESS
            access.grfInheritance = 0
            access.Trustee.TrusteeForm = 0  # TRUSTEE_IS_SID
            access.Trustee.TrusteeType = 0  # TRUSTEE_IS_UNKNOWN
            access.Trustee.ptstrName = slot.sid_ptr
            if advapi.SetEntriesInAclW(1, ctypes.byref(access), dacl, ctypes.byref(new_dacl)) != 0:
                raise SandboxUnavailable("cannot build the %s DACL" % what)
            if advapi.SetSecurityInfo(handle, 7, 4, None, None, new_dacl, None) != 0:
                raise SandboxUnavailable("cannot apply the %s DACL" % what)
        finally:
            if new_dacl.value:
                k32.LocalFree(new_dacl)
            if descriptor.value:
                k32.LocalFree(descriptor)

    def make_desktop():
        """A private, empty desktop in the current window station that only this slot's SID may use.

        The sandboxed process therefore cannot see the interactive desktop's windows or pixels.
        """
        station = user32.GetProcessWindowStation()
        text = ctypes.create_unicode_buffer(256)
        user32.GetUserObjectInformationW(station, 2, text, 512, None)
        desktop_name = "sonder-meshgen-" + uuid.uuid4().hex[:12]
        desktop = user32.CreateDesktopW(desktop_name, None, None, 0, 0x10000000, None)  # GENERIC_ALL
        if not desktop:
            raise SandboxUnavailable("CreateDesktop failed: winerror %d" % ctypes.get_last_error())
        try:
            grant_slot(desktop, "desktop", 0x10000000)
        except BaseException:
            user32.CloseDesktop(desktop)
            raise
        return desktop, text.value + chr(92) + desktop_name

    cwd = str(cwd)
    stdout_path = os.path.join(cwd, "stdout.txt")
    stderr_path = os.path.join(cwd, "stderr.txt")
    job = None
    attr_buffer = None
    attr_ready = False
    desktop = None
    process = thread = None
    try:
        hin = open_inheritable("NUL", False)
        hout = open_inheritable(stdout_path, True)
        herr = open_inheritable(stderr_path, True)

        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise SandboxUnavailable("CreateJobObject failed: winerror %d" % ctypes.get_last_error())
        limit = JobExtended()
        # KILL_ON_JOB_CLOSE | ACTIVE_PROCESS | JOB_MEMORY
        limit.Basic.LimitFlags = 0x2000 | 0x8 | 0x200
        limit.Basic.ActiveProcessLimit = 1
        limit.JobMemoryLimit = int(memory_mb) * 1024 * 1024
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
            raise SandboxUnavailable("SetInformationJobObject failed: winerror %d" % ctypes.get_last_error())

        desktop, desktop_name = make_desktop()

        count = 2  # HANDLE_LIST, SECURITY_CAPABILITIES
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))
        attr_buffer = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attr_buffer, count, 0, ctypes.byref(size)):
            raise SandboxUnavailable("InitializeProcThreadAttributeList failed")
        attr_ready = True
        handles = (W.HANDLE * 3)(hin, hout, herr)
        if not k32.UpdateProcThreadAttribute(attr_buffer, 0, 0x00020002, handles, ctypes.sizeof(handles), None, None):
            raise SandboxUnavailable("HANDLE_LIST attribute failed")
        security = SecurityCapabilities(slot.sid_ptr, None, 0, 0)  # zero capabilities
        if not k32.UpdateProcThreadAttribute(attr_buffer, 0, 0x00020009, ctypes.byref(security),
                                             ctypes.sizeof(security), None, None):
            raise SandboxUnavailable("SECURITY_CAPABILITIES attribute failed")
        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.lpDesktop = desktop_name
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = hin, hout, herr
        startup.lpAttributeList = ctypes.cast(attr_buffer, ctypes.c_void_p)
        info = ProcessInformation()
        block = "".join("%s=%s\0" % item for item in sorted(env.items())) + "\0"
        env_buffer = ctypes.create_unicode_buffer(block, len(block))
        flags = 0x00080000 | 0x4 | 0x400 | 0x08000000  # EXTENDED_STARTUPINFO | SUSPENDED | UNICODE_ENV | NO_WINDOW
        if not k32.CreateProcessW(None, ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv))), None,
                                  None, True, flags, env_buffer, cwd, ctypes.byref(startup), ctypes.byref(info)):
            raise SandboxUnavailable("sandboxed launch failed: winerror %d" % ctypes.get_last_error())
        process, thread = info.hProcess, info.hThread
        if not k32.AssignProcessToJobObject(job, process):
            error = ctypes.get_last_error()
            k32.TerminateProcess(process, 125)
            raise SandboxUnavailable("AssignProcessToJobObject failed (winerror %d); not running unconfined" % error)
        if k32.ResumeThread(thread) == 0xFFFFFFFF:
            k32.TerminateProcess(process, 125)
            raise SandboxUnavailable("ResumeThread failed")

        status = k32.WaitForSingleObject(process, int(max(1, timeout) * 1000))
        if status == 0xFFFFFFFF:  # WAIT_FAILED
            k32.TerminateProcess(process, 125)
            raise SandboxUnavailable("waiting for the sandboxed process failed (winerror %d)" % ctypes.get_last_error())
        timed_out = status != 0
        if timed_out:
            k32.TerminateProcess(process, 124)
            k32.WaitForSingleObject(process, 5000)
        code = W.DWORD()
        k32.GetExitCodeProcess(process, ctypes.byref(code))
        accounting = JobExtended()
        peak = 0
        if k32.QueryInformationJobObject(job, 9, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            peak = int(accounting.PeakJobMemoryUsed)
        for handle in opened:
            k32.CloseHandle(handle)
        opened.clear()
        return SandboxRun(int(code.value), tail_bytes(stdout_path), tail_bytes(stderr_path), timed_out,
                          (not timed_out) and peak >= int(0.9 * limit.JobMemoryLimit),
                          peak // (1024 * 1024), int(memory_mb))
    finally:
        if attr_ready:
            k32.DeleteProcThreadAttributeList(attr_buffer)
        for handle in opened:
            k32.CloseHandle(handle)
        if process:
            k32.TerminateProcess(process, 125)  # no-op if it already exited
            k32.CloseHandle(process)
        if thread:
            k32.CloseHandle(thread)
        if job:
            k32.CloseHandle(job)  # KILL_ON_JOB_CLOSE reaps anything left
        if desktop:
            user32.CloseDesktop(desktop)
