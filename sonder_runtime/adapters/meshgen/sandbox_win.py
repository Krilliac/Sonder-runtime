"""Windows AppContainer sandbox for the headless Blender that runs model-written scripts.

This module is the security boundary of ``mesh_generate``.  The AST gate and the restricted
builtins in the harness are cheap first filters that catch mistakes; they are not what stops a
hostile script.  What stops it is that Blender runs as a *zero-capability AppContainer* process:

* it can read and write only inside its own per-build run directory (plus read the Blender
  install directory), so user files, the repository and credential stores are unreadable;
* it has no network capability (loopback included) and cannot start a second process
  (``ActiveProcessLimit=1`` in the Job Object);
* the Job Object also caps job memory and kills the process tree when the handle closes.

Footprint on the machine (documented in ``docs/runbooks/mesh-generation.md``):

* an AppContainer profile named ``Sonder.MeshGen`` (per user, created on first use);
* ONE persistent ACL entry: ``(OI)(CI)(RX)`` for that profile's SID on the Blender install
  directory, added only when missing.  Undo with ``icacls <blender dir> /remove:g *<SID>``;
* per build, a fresh run directory with a ``(OI)(CI)(M)`` grant that is removed (and the
  directory deleted) when the build ends.  No ACL is ever placed on the kit's source directory,
  the repository or any user directory.

Pure helpers (environment, grant planning, mode selection) are separate from the ctypes launch
so they can be unit tested anywhere.  Thread affinity: every call is self-contained; builds run
concurrently, each with its own run directory, grant, job and handles.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

PROFILE_NAME = "Sonder.MeshGen"
OPT_OUT_ENV = "SONDER_MESHGEN_UNSANDBOXED"
MEMORY_ENV = "SONDER_MESHGEN_MEM_MB"
DEFAULT_MEMORY_MB = 4096
MIN_MEMORY_MB = 256
MAX_MEMORY_MB = 65536
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
    """``appcontainer`` on Windows unless the operator explicitly opted out; else ``unsandboxed``.

    Non-Windows platforms have no sandbox implementation here (documented gap), so they report
    ``unsandboxed`` honestly instead of pretending.
    """
    if os_name != "nt":
        return UNSANDBOXED
    return UNSANDBOXED if opt_out_requested(env) else SANDBOXED


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
        norm = _norm(other)
        if run == norm or _overlaps(run, norm) and os.path.commonpath([run, norm]) == run:
            raise SandboxUnavailable("run directory %s would cover the protected directory %s" % (run, other))
    return [Grant(str(run_dir), "M", False), Grant(str(blender_dir), "RX", True)]


# --------------------------------------------------------------------------- Windows plumbing

def _icacls(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls"] + args, capture_output=True, text=True, timeout=60, check=False,
                          stdin=subprocess.DEVNULL)


_PROFILE_LOCK = threading.Lock()
_SID_CACHE: dict = {}


def _container_sid_string() -> tuple[int, str]:
    """Create the profile (or derive its SID when it exists); returns (PSID address, SID string)."""
    from ctypes import wintypes as W

    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    userenv.CreateAppContainerProfile.argtypes = [W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD,
                                                  ctypes.POINTER(ctypes.c_void_p)]
    userenv.CreateAppContainerProfile.restype = ctypes.c_long
    userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(W.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = W.BOOL
    psid = ctypes.c_void_p()
    hr = userenv.CreateAppContainerProfile(PROFILE_NAME, "Sonder MeshGen", "Sonder mesh_generate sandbox",
                                           None, 0, ctypes.byref(psid))
    if (hr & 0xFFFFFFFF) == 0x800700B7:  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
        hr = userenv.DeriveAppContainerSidFromAppContainerName(PROFILE_NAME, ctypes.byref(psid))
    if hr != 0:
        raise SandboxUnavailable("AppContainer profile %s unavailable (hr=0x%08X)" % (PROFILE_NAME, hr & 0xFFFFFFFF))
    text = W.LPWSTR()
    if not advapi.ConvertSidToStringSidW(psid, ctypes.byref(text)):
        raise SandboxUnavailable("could not read the AppContainer SID")
    return int(psid.value), text.value


def container_sid() -> tuple[int, str]:
    with _PROFILE_LOCK:
        if "sid" not in _SID_CACHE:
            _SID_CACHE["sid"] = _container_sid_string()
        return _SID_CACHE["sid"]


def ensure_blender_access(blender_dir, sid: str | None = None) -> bool:
    """Grant the container Read+Execute on the Blender install dir if (and only if) it is missing.

    Idempotent and done once per directory per process.  Returns True when a grant was added.
    """
    sid = sid or container_sid()[1]
    key = ("blender", _norm(blender_dir))
    with _PROFILE_LOCK:
        if _SID_CACHE.get(key):
            return False
        shown = _icacls([str(blender_dir)])
        if shown.returncode == 0 and sid in shown.stdout:
            _SID_CACHE[key] = True
            return False
        done = _icacls([str(blender_dir), "/grant", "*%s:(OI)(CI)(RX)" % sid])
        if done.returncode != 0:
            raise SandboxUnavailable(
                "cannot grant the sandbox read access to %s (%s). Run once as an account that can edit its "
                "ACL: icacls \"%s\" /grant *%s:(OI)(CI)(RX)"
                % (blender_dir, (done.stderr or done.stdout).strip()[:200], blender_dir, sid))
        _SID_CACHE[key] = True
        return True


def prepare(blender_dir) -> str:
    """Make sure the profile and the Blender grant exist; returns the SID string. Fails closed."""
    if os.name != "nt":
        raise SandboxUnavailable("the AppContainer sandbox exists only on Windows")
    try:
        sid = container_sid()[1]
        ensure_blender_access(blender_dir, sid)
    except SandboxUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any setup failure must surface as "unavailable"
        raise SandboxUnavailable("sandbox setup failed: %s: %s" % (type(exc).__name__, exc)) from exc
    return sid


@contextlib.contextmanager
def granted_run_dir(base, blender_dir, kit_source_dir=None, protected=()):
    """A fresh run directory under ``base`` with a Modify grant for this build only.

    The grant is removed and the directory deleted on exit, success or failure.
    """
    sid = prepare(blender_dir)
    run = Path(base) / (".run-" + uuid.uuid4().hex[:12])
    plan = plan_grants(run, blender_dir, kit_source_dir, protected)
    run.mkdir(parents=True, exist_ok=False)
    try:
        done = _icacls([str(run), "/grant", "*%s:(OI)(CI)(%s)" % (sid, plan[0].permission)])
        if done.returncode != 0:
            raise SandboxUnavailable("cannot grant the sandbox access to its run directory: %s"
                                     % (done.stderr or done.stdout).strip()[:200])
        for directory in environment_directories(run):
            directory.mkdir(parents=True, exist_ok=True)
        yield run, sid
    finally:
        _icacls([str(run), "/remove:g", "*%s" % sid])
        shutil.rmtree(run, ignore_errors=True)


@dataclass
class SandboxRun:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    memory_exceeded: bool
    peak_job_mb: int
    memory_limit_mb: int


def run_sandboxed(argv, *, cwd, env, timeout, sid_ptr: int, memory_mb: int) -> SandboxRun:
    """Launch ``argv`` suspended in the AppContainer, inside a limited Job Object, then resume it.

    The process is resumed only after ``AssignProcessToJobObject`` succeeded; if that fails the
    process is terminated and ``SandboxUnavailable`` is raised (fail closed).
    """
    from ctypes import wintypes as W

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

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

    cwd = str(cwd)
    stdout_path = os.path.join(cwd, "stdout.txt")
    stderr_path = os.path.join(cwd, "stderr.txt")
    job = None
    attr_buffer = None
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

        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        attr_buffer = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attr_buffer, 2, 0, ctypes.byref(size)):
            raise SandboxUnavailable("InitializeProcThreadAttributeList failed")
        handles = (W.HANDLE * 3)(hin, hout, herr)
        if not k32.UpdateProcThreadAttribute(attr_buffer, 0, 0x00020002, handles, ctypes.sizeof(handles), None, None):
            raise SandboxUnavailable("HANDLE_LIST attribute failed")
        capabilities = SecurityCapabilities(sid_ptr, None, 0, 0)
        if not k32.UpdateProcThreadAttribute(attr_buffer, 0, 0x00020009, ctypes.byref(capabilities),
                                             ctypes.sizeof(capabilities), None, None):
            raise SandboxUnavailable("SECURITY_CAPABILITIES attribute failed")

        startup = StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(StartupInfoEx)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
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

        timed_out = k32.WaitForSingleObject(process, int(max(1, timeout) * 1000)) != 0
        if timed_out:
            k32.TerminateProcess(process, 124)
            k32.WaitForSingleObject(process, 5000)
        code = W.DWORD()
        k32.GetExitCodeProcess(process, ctypes.byref(code))
        accounting = JobExtended()
        peak = 0
        if k32.QueryInformationJobObject(job, 9, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            peak = int(accounting.PeakJobMemoryUsed)
        peak_mb = peak // (1024 * 1024)
        exceeded = (not timed_out) and peak >= int(0.9 * limit.JobMemoryLimit)  # reported only when the build failed
        for handle in opened:
            k32.CloseHandle(handle)
        opened.clear()
        out = Path(stdout_path).read_bytes() if os.path.isfile(stdout_path) else b""
        err = Path(stderr_path).read_bytes() if os.path.isfile(stderr_path) else b""
        return SandboxRun(int(code.value), out, err, timed_out, exceeded, peak_mb, int(memory_mb))
    finally:
        if attr_buffer is not None:
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
