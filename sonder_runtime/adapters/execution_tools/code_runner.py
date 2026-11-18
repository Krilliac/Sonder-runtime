"""Small bounded code runner exposed through the MCP server.

This is intentionally lightweight: it is not a security sandbox. It gives an
agent a Claude-like way to execute short local snippets while keeping the
interface predictable: known languages, workspace-confined cwd, timeout, and
trimmed output.
"""
import os
import ctypes
import ctypes.wintypes
import glob
import json
import locale
import logging
import ntpath
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from sonder_runtime.platform import logging as sonder_logging
from sonder_runtime.platform import paths as sonder_paths
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread
from sonder_runtime.application.workflows import loop as workflow_loop

try:  # POSIX only: RunLimits sets the child's rlimits through it
    import resource as _resource
except ImportError:
    _resource = None

_REAL_POPEN = subprocess.Popen


SUPPORTED_LANGUAGES = {
    "python": {
        "aliases": {"python", "py"},
        "suffix": ".py",
        "cmd": lambda path: [sys.executable, path],
        "missing": "python executable not available",
    },
    "javascript": {
        "aliases": {"javascript", "js", "node"},
        "suffix": ".js",
        "cmd": lambda path: ["node", path],
        "missing": "node executable not found on PATH",
    },
    "powershell": {
        "aliases": {"powershell", "pwsh", "ps1"},
        "suffix": ".ps1",
        "cmd": lambda path: [_powershell_exe(), "-NoProfile", "-File", path],
        "missing": "PowerShell executable not found on PATH",
    },
    "cpp": {
        "aliases": {"cpp", "c++", "cc", "cxx"},
        "suffix": ".cpp",
        "missing": "C++ compiler not found (tried g++, clang++, cl, and Visual Studio vcvars64.bat)",
    },
    "csharp": {
        "aliases": {"csharp", "cs", "c#"},
        "suffix": ".cs",
        "missing": ".NET SDK or C# compiler not found on PATH (tried dotnet, csc)",
    },
    "bash": {
        "aliases": {"bash", "sh", "shell", "zsh"},
        "suffix": ".sh",
        "cmd": lambda path: [sonder_paths.bash_executable() or "bash", path],
        "missing": "bash/sh executable not found on PATH",
    },
    "ruby": {
        "aliases": {"ruby", "rb"},
        "suffix": ".rb",
        "cmd": lambda path: ["ruby", path],
        "missing": "ruby executable not found on PATH",
    },
    "perl": {
        "aliases": {"perl", "pl"},
        "suffix": ".pl",
        "cmd": lambda path: ["perl", path],
        "missing": "perl executable not found on PATH",
    },
    "php": {
        "aliases": {"php"},
        "suffix": ".php",
        "cmd": lambda path: ["php", path],
        "missing": "php executable not found on PATH",
    },
    "lua": {
        "aliases": {"lua"},
        "suffix": ".lua",
        "cmd": lambda path: [shutil.which("lua") or shutil.which("lua5.4") or shutil.which("lua5.3") or "lua", path],
        "missing": "lua executable not found on PATH",
    },
    "r": {
        "aliases": {"r", "rscript"},
        "suffix": ".R",
        "cmd": lambda path: ["Rscript", path],
        "missing": "Rscript executable not found on PATH",
    },
    "go": {
        "aliases": {"go", "golang"},
        "suffix": ".go",
        # `go run` compiles and executes in one bounded step.
        "cmd": lambda path: ["go", "run", path],
        "missing": "go toolchain not found on PATH",
    },
    "java": {
        "aliases": {"java"},
        "suffix": ".java",
        # JDK 11+ single-file source launcher; no separate compile step.
        "cmd": lambda path: ["java", path],
        "missing": "java (JDK 11+) not found on PATH",
    },
    "typescript": {
        "aliases": {"typescript", "ts"},
        "suffix": ".ts",
        # Node 22.6+ strips type annotations natively; older nodes report
        # their own actionable error through stderr.
        "cmd": lambda path: ["node", "--experimental-strip-types", path],
        "missing": "node executable not found on PATH (TypeScript runs via node --experimental-strip-types)",
    },
    "rust": {
        "aliases": {"rust", "rs"},
        "suffix": ".rs",
        "missing": "rustc not found on PATH",
    },
}

DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 60
MAX_OUTPUT_CHARS = 12000
# Bytes of each child pipe _collect_output keeps: output up to this size is
# returned exactly as communicate() did (whole, then _trim_output's tail
# cut); past it a head and tail are kept and the middle is drained and
# discarded. 5 x MAX_OUTPUT_CHARS leaves room for utf-8's 4 bytes per char.
DEFAULT_CAPTURE_BYTES = 5 * MAX_OUTPUT_CHARS
_READ_CHUNK_BYTES = 65536
_CREATE_SUSPENDED = 0x00000004
# How often a run with an output kill threshold checks it while waiting.
_OUTPUT_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class RunLimits:
    """Opt-in hardening for one run_code call (the chat run_code lane).

    Immutable, so one instance may be shared across threads. Callers that
    pass nothing keep today's behaviour exactly. With limits the child is
    started suspended and placed in a Job Object capped at ``process_limit``
    active processes and ``job_memory_bytes`` of committed memory before it
    runs (Windows; the run is refused if the job cannot be configured), or
    runs under RLIMIT_NPROC / RLIMIT_AS / RLIMIT_FSIZE (POSIX). Python runs
    with -I when ``isolated_python``. Each output stream keeps at most
    ``capture_bytes_per_stream`` bytes, and the tree is killed once the two
    streams together pass ``kill_after_total_bytes``. This is not a sandbox:
    there is no network or filesystem confinement beyond the temp cwd.
    """

    process_limit: int = 16
    job_memory_bytes: int = 512 * 1024 * 1024
    file_size_bytes: int = 64 * 1024 * 1024
    isolated_python: bool = True
    capture_bytes_per_stream: int = 64 * 1024
    kill_after_total_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        for name in (
            "process_limit", "job_memory_bytes", "file_size_bytes",
            "capture_bytes_per_stream", "kill_after_total_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError("RunLimits.%s must be a positive int" % name)
        if type(self.isolated_python) is not bool:
            raise ValueError("RunLimits.isolated_python must be a bool")
DEFAULT_LOOP_ITERATIONS = 5
MAX_LOOP_ITERATIONS = 50
MAX_LOOP_DELAY_SECONDS = 10.0
MAX_PROJECT_FILES = 80
MAX_PROJECT_BYTES = 750000
# Each run_project step gets the full timeout, so the step count bounds the
# whole call: 16 x MAX_TIMEOUT.
MAX_PROJECT_COMMANDS = 16
RUN_WINDOW_DIR_ENV = "SONDER_RUN_WINDOW_DIR"
# Lifecycle quota for detached /runwindow consoles.  ``timeout`` bounds only
# the compile/launch step; these bound what keeps running afterwards: how
# many consoles may be open at once, how long each (with every process it
# started) may live, how many processes its Job Object admits, and how many
# generated run directories are retained on disk.
RUN_WINDOW_MAX_LIVE = 4
RUN_WINDOW_LIFETIME_SECONDS = 3600
RUN_WINDOW_MAX_PROCESSES = 32
RUN_WINDOW_MAX_RETAINED_DIRS = 16
_RUN_WINDOW_PREFIX = "sonder-window-"


def _child_environment():
    # Model-authored children used to inherit bypass/API credentials and could
    # print them into tool output, granting themselves control-plane authority.
    # The stripping rule now lives in sonder_logging so this lane and
    # workbench's run_program cannot diverge -- they did, and workbench leaked
    # the whole environment to the same class of model-authored child.
    environment = sonder_logging.child_environment()
    # Windows defaults a redirected Python child to the active ANSI code page
    # (commonly cp1252).  Generated programs routinely print Unicode, so that
    # turns a valid snippet such as ``print("🎲")`` into a false execution
    # failure.  Make the model-code lane consistently UTF-8 while retaining
    # the shared secret-stripping policy above.
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _powershell_exe():
    return shutil.which("pwsh") or shutil.which("powershell") or "powershell"


def workspace_root():
    """Sonder's checkout, the base for relative ``cwd`` values.

    ``dirname(__file__)`` stopped naming the checkout when this module moved
    into ``sonder_runtime/adapters/execution_tools``: ``resolve_cwd("tests")``
    failed and ``cwd="."`` ran the child inside the runtime package. file_ops
    owns the checkout-root rule (it fixed the same regression for itself), so
    defer to it. Imported here, not at module level, to keep this module's
    import light and free of cycles.
    """
    from sonder_runtime.adapters.filesystem import file_ops

    return str(file_ops.workspace_root())


def normalize_language(language):
    wanted = (language or "python").strip().lower()
    for canonical, cfg in SUPPORTED_LANGUAGES.items():
        if wanted in cfg["aliases"]:
            return canonical
    raise ValueError(
        "unsupported language %r. Supported: %s"
        % (language, ", ".join(sorted(SUPPORTED_LANGUAGES)))
    )


def resolve_cwd(cwd=None):
    # Resolve symlinks on both the workspace root and the candidate cwd before
    # the containment check (via os.path.realpath), mirroring file_ops.resolve_path
    # which uses Path.resolve(). Without this, a symlink living inside the
    # workspace but pointing outside it would pass an abspath+commonpath check and
    # place the child process's cwd outside the tree. This is a tidiness boundary,
    # not a sandbox (see module docstring); for non-symlinked paths the result is
    # identical to the previous os.path.abspath behavior.
    root = os.path.realpath(workspace_root())
    if not cwd:
        return root
    path = cwd
    if not os.path.isabs(path):
        path = os.path.join(root, path)
    path = os.path.realpath(path)
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("cwd must stay inside workspace: %r" % cwd)
    if not os.path.isdir(path):
        raise ValueError("cwd does not exist: %r" % cwd)
    return path


def _clamp_timeout(timeout):
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return max(1, min(value, MAX_TIMEOUT))


def _trim_output(text, limit=MAX_OUTPUT_CHARS):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _completed_result(proc, language, cwd, timeout, error=""):
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _trim_output(proc.stdout),
        "stderr": _trim_output(proc.stderr),
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": error,
    }


def _error_result(language, cwd, timeout, error):
    return {
        "ok": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": error,
    }


def _timeout_result(language, cwd, timeout, exc):
    return {
        "ok": False,
        "returncode": None,
        "stdout": _trim_output(exc.stdout if isinstance(exc.stdout, str) else ""),
        "stderr": _trim_output(exc.stderr if isinstance(exc.stderr, str) else ""),
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": "timed out after %ss" % timeout,
    }


def _terminate_process_tree(proc):
    """Best-effort teardown for a timed-out model-authored process tree.

    ``subprocess.run(..., timeout=...)`` terminates only the direct child.  A
    snippet can therefore spawn a background process and let it continue after
    the runner has reported the timeout.  Start every normal runner command in
    its own process group and tear down that whole group here.  This is still
    not an OS sandbox, but it makes the advertised execution deadline honest
    for ordinary descendants on both supported host families.
    """
    if os.name == "nt":
        job = getattr(proc, "_sonder_job_handle", None)
        if job:
            try:
                ctypes.windll.kernel32.TerminateJobObject(job, 1)
                _close_windows_job(job)
                proc._sonder_job_handle = None
                return
            except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
                pass
        try:
            _terminate_windows_descendants(proc.pid)
        except (AttributeError, OSError):
            pass
        # ``taskkill /T`` uses the process parent tree rather than a shell, and
        # the PID came from our own Popen call.  It is intentionally a bounded
        # best effort: an already-exited parent simply makes the cleanup a no-op.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                env=_child_environment(),
                check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _terminate_windows_descendants(root_pid):
    """Terminate descendants by parent PID when Job Objects are unavailable."""
    kernel32 = ctypes.windll.kernel32

    class Entry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ProcessID", ctypes.wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.wintypes.DWORD),
            ("cntThreads", ctypes.wintypes.DWORD),
            ("th32ParentProcessID", ctypes.wintypes.DWORD),
            ("pcPriClassBase", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szExeFile", ctypes.wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32FirstW.restype = ctypes.wintypes.BOOL
    kernel32.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel32.Process32NextW.restype = ctypes.wintypes.BOOL
    kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.wintypes.HANDLE(-1).value:
        return
    try:
        rows = []
        row = Entry(); row.dwSize = ctypes.sizeof(Entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(row)):
            while True:
                rows.append((int(row.th32ProcessID), int(row.th32ParentProcessID)))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(row)):
                    break
        children = {}
        for pid, parent in rows:
            children.setdefault(parent, []).append(pid)
        pending = list(children.get(int(root_pid), ()))
        descendants = []
        while pending:
            pid = pending.pop()
            descendants.append(pid)
            pending.extend(children.get(pid, ()))
        for pid in reversed(descendants):
            handle = kernel32.OpenProcess(1, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 1)
                kernel32.CloseHandle(handle)
    finally:
        kernel32.CloseHandle(snapshot)


def _attach_windows_job(proc, *, active_process_limit=None, job_memory_bytes=None):
    """Put a runner process in a kill-on-close Windows Job Object.

    ``active_process_limit`` additionally caps how many processes the job
    may hold at once (JOB_OBJECT_LIMIT_ACTIVE_PROCESS) and
    ``job_memory_bytes`` the memory all of them may commit together
    (JOB_OBJECT_LIMIT_JOB_MEMORY). Returns None when any of it cannot be
    set (SetInformationJobObject's result is checked), so a caller that
    needs the limits can refuse to run.
    """
    if os.name != "nt":
        return None
    process_handle = getattr(proc, "_handle", None)
    if process_handle is None:
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.wintypes.LPVOID, ctypes.wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
            ctypes.wintypes.LPVOID, ctypes.wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.wintypes.HANDLE, ctypes.wintypes.HANDLE,
        ]
        kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.wintypes.DWORD),
                ("SchedulingClass", ctypes.wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if active_process_limit is not None:
            info.BasicLimitInformation.LimitFlags |= 0x0008  # ACTIVE_PROCESS
            info.BasicLimitInformation.ActiveProcessLimit = max(1, int(active_process_limit))
        if job_memory_bytes is not None:
            info.BasicLimitInformation.LimitFlags |= 0x0200  # JOB_MEMORY
            info.JobMemoryLimit = max(1, int(job_memory_bytes))
        kernel32.SetInformationJobObject.restype = ctypes.wintypes.BOOL
        kernel32.AssignProcessToJobObject.restype = ctypes.wintypes.BOOL
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, process_handle):
            kernel32.CloseHandle(job)
            return None
        return job
    except (AttributeError, OSError, TypeError):
        return None


def _close_windows_job(job):
    if job and os.name == "nt":
        try:
            ctypes.windll.kernel32.CloseHandle(job)
        except (AttributeError, OSError, TypeError, ctypes.ArgumentError):
            # ArgumentError: a handle ctypes cannot marshal (it is not an
            # OSError). Closing is best effort; never raise out of teardown,
            # which runs on the lifetime timer thread.
            pass


def _resume_windows_process(proc):
    """Resume the initial thread of a process started with CREATE_SUSPENDED."""
    kernel32 = ctypes.windll.kernel32

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.wintypes.DWORD),
            ("cntUsage", ctypes.wintypes.DWORD),
            ("th32ThreadID", ctypes.wintypes.DWORD),
            ("th32OwnerProcessID", ctypes.wintypes.DWORD),
            ("tpBasePri", ctypes.wintypes.LONG),
            ("tpDeltaPri", ctypes.wintypes.LONG),
            ("dwFlags", ctypes.wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    kernel32.Thread32First.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel32.Thread32First.restype = ctypes.wintypes.BOOL
    kernel32.Thread32Next.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel32.Thread32Next.restype = ctypes.wintypes.BOOL
    kernel32.OpenThread.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                   ctypes.wintypes.DWORD]
    kernel32.OpenThread.restype = ctypes.wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.ResumeThread.restype = ctypes.wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError("could not enumerate suspended console threads")
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(ThreadEntry)
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == proc.pid:
                thread = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)
                if not thread:
                    break
                try:
                    if kernel32.ResumeThread(thread) != 1:
                        raise OSError("could not resume suspended console thread")
                    return
                finally:
                    kernel32.CloseHandle(thread)
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("could not find suspended console thread")
    finally:
        kernel32.CloseHandle(snapshot)


def _decode_child_output(raw):
    """Decode a child's stdout/stderr without losing output to either encoding.

    Two populations share this lane. Python children are forced to utf-8 by
    _child_environment(), and node and git-bash emit utf-8 regardless -- decoding
    those with the host locale produces mojibake that still looks like output, so
    nothing errors and only a reader notices. But PHP, Perl, native executables
    and localized compilers follow the host's ANSI/OEM code page, and decoding
    *those* as utf-8 with errors="replace" would turn a valid cp1252 byte such as
    0xE9 into U+FFFD -- destroying output the old locale-based decoding kept.

    Neither encoding is right for every child, so try the strict one first: valid
    utf-8 is decoded as utf-8, and anything that is not valid utf-8 could not have
    come from a utf-8 child, so it falls back to the host encoding. Only bytes
    that are invalid in *both* are replaced.
    """
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False), errors="replace")


def _utf8_cut_end(data):
    """Drop a utf-8 sequence the head capture cut in half (so it decodes)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 == 0x80:
            continue
        if byte >= 0xC0:
            need = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if need > back:
                return data[:-back]
        break
    return data


def _utf8_cut_start(data):
    """Drop continuation bytes left at the start of a rolled tail capture."""
    skip = 0
    while skip < min(3, len(data)) and data[skip] & 0xC0 == 0x80:
        skip += 1
    return data[skip:]


class _StreamCapture:
    """Bounded capture of one child pipe: the first and last bytes it wrote.

    Fed by exactly one reader thread and read by the runner thread; the lock
    covers both, because after a timeout the reader may still be draining a
    pipe a surviving descendant holds while the runner builds its result.
    Memory is bounded by ``capture_bytes`` plus one read chunk.
    """

    def __init__(self, capture_bytes):
        self._lock = threading.Lock()
        self._head_limit = capture_bytes // 5
        self._tail_limit = capture_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total = 0

    def feed(self, chunk):
        with self._lock:
            self._total += len(chunk)
            room = self._head_limit - len(self._head)
            if room > 0:
                self._head += chunk[:room]
                chunk = chunk[room:]
            if chunk:
                self._tail += chunk
                excess = len(self._tail) - self._tail_limit
                if excess > 0:
                    del self._tail[:excess]

    def text(self):
        """Decoded output; past the capture, head + marker + tail in MAX_OUTPUT_CHARS."""
        with self._lock:
            head, tail, total = bytes(self._head), bytes(self._tail), self._total
        if total == len(head) + len(tail):
            return _decode_child_output(head + tail)
        marker = (
            "\n[... output truncated: %d bytes written, showing the start and end ...]\n"
            % total
        )
        head_text = _decode_child_output(_utf8_cut_end(head))[:MAX_OUTPUT_CHARS // 4]
        room = max(0, MAX_OUTPUT_CHARS - len(head_text) - len(marker))
        tail_text = _decode_child_output(_utf8_cut_start(tail))
        return head_text + marker + (tail_text[-room:] if room else "")


class _OutputBudget:
    """Total bytes both pipes of one run wrote, against a kill threshold.

    Shared by the two reader threads (hence the lock); ``exceeded`` tells
    the runner thread to kill the tree.
    """

    def __init__(self, limit):
        self._lock = threading.Lock()
        self._limit = limit
        self._total = 0
        self.exceeded = threading.Event()

    def add(self, count):
        with self._lock:
            self._total += count
            if self._total > self._limit:
                self.exceeded.set()


def _drain_into(stream, capture, budget=None):
    read = getattr(stream, "read1", stream.read)
    try:
        while True:
            chunk = read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            capture.feed(chunk)
            if budget is not None:
                budget.add(len(chunk))
    except (OSError, ValueError):
        return


def _feed_stdin(stream, data):
    try:
        if data:
            stream.write(data)
    except (OSError, ValueError):
        pass  # the child exited or closed stdin without reading it all
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _wait_watching_output(proc, deadline, budget):
    """Wait for exit until ``deadline``; "output" once ``budget`` is exceeded."""
    while True:
        remaining = deadline - time.monotonic()
        if budget.exceeded.wait(max(0.0, min(_OUTPUT_POLL_SECONDS, remaining))):
            return "output"
        if proc.poll() is not None:
            return None
        if time.monotonic() >= deadline:
            return "timeout"


def _collect_output(
    proc, input_bytes, timeout, *, capture_bytes=DEFAULT_CAPTURE_BYTES,
    kill_after_bytes=None,
):
    """Feed stdin, wait for exit and collect output: (stdout, stderr, status).

    ``status`` is None for a run that finished, "timeout", or "output" when
    the streams together passed ``kill_after_bytes`` and the tree was killed
    (only when a threshold is given; by default the excess is discarded).

    Replaces ``communicate()``, which held every byte a child printed until it
    exited: a snippet printing gigabytes exhausted host memory before
    _trim_output ever ran. Each pipe is drained into a bounded
    _StreamCapture, and the child keeps running to completion (the excess is
    discarded rather than the child killed), so a chatty but correct program
    still reports its real exit status; the timeout bounds how long it runs.

    The deadline covers what communicate() covered: the child exiting *and*
    both pipes closing, so a descendant still holding a pipe keeps the run
    open until the deadline. On timeout the tree is torn down and the output
    is what was printed in time.
    """
    deadline = time.monotonic() + timeout
    captures = (_StreamCapture(capture_bytes), _StreamCapture(capture_bytes))
    budget = None if kill_after_bytes is None else _OutputBudget(kill_after_bytes)
    readers = []
    for stream, capture in zip((proc.stdout, proc.stderr), captures, strict=True):
        if stream is not None:
            reader = owned_runtime_thread(
                target=_drain_into, args=(stream, capture, budget),
                name="sonder-code-runner-reader", daemon=True,
            )
            reader.start()
            readers.append((reader, stream))
    if proc.stdin is not None:
        owned_runtime_thread(
            target=_feed_stdin, args=(proc.stdin, input_bytes),
            name="sonder-code-runner-stdin", daemon=True,
        ).start()
    status = None
    if budget is not None:
        status = _wait_watching_output(proc, deadline, budget)
    else:
        try:
            # The child gets the whole timeout; only the pipe joins use the
            # deadline, which starting the threads moved by microseconds.
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            status = "timeout"
    if status is None:
        for reader, _stream in readers:
            reader.join(max(0.0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader, _stream in readers):
            status = "timeout"
    if status is not None:
        _terminate_process_tree(proc)
        # ``taskkill /T`` can report success before the process handle is
        # signalled.  Waiting on the direct handle first prevents the
        # TemporaryDirectory owner from racing a still-live interpreter that
        # has inherited the snippet directory or pipe handles.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        # Collect what is still in the pipes, as communicate(timeout=1) did.
        late = time.monotonic() + 1
        for reader, _stream in readers:
            reader.join(max(0.0, late - time.monotonic()))
    for reader, stream in readers:
        if not reader.is_alive():  # a live reader still owns its pipe
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    return captures[0].text(), captures[1].text(), status


def _abort_process(proc):
    """Tear down and reap ``proc``'s tree after a failure past the spawn.

    The pipes are left to the reader threads and garbage collection: closing
    a buffered pipe blocks on the lock a reader holds while it waits in read.
    """
    try:
        _terminate_process_tree(proc)
    except Exception:  # noqa: BLE001 - keep going: the job close below still kills the tree
        logging.getLogger(__name__).warning("runner teardown failed", exc_info=True)
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _release_job(proc):
    """Close ``proc``'s Job Object exactly once (kill-on-close ends the tree)."""
    _close_windows_job(getattr(proc, "_sonder_job_handle", None))
    try:
        proc._sonder_job_handle = None
    except AttributeError:
        pass


def _limited_windows_job(proc, limits):
    """The Job Object for a RunLimits run, or None if it cannot be configured."""
    return _attach_windows_job(
        proc, active_process_limit=limits.process_limit,
        job_memory_bytes=limits.job_memory_bytes,
    )


def _posix_limit_preexec(limits, resource_module=None):
    """Build the preexec_fn that applies RunLimits' rlimits in the child.

    Everything that can fail is resolved here, in the parent: a missing
    RLIMIT_* raises OSError and the run is refused. The returned function
    only calls getrlimit/setrlimit (C calls, no Python locks), which keeps
    it safe to run between fork and exec in this multi-threaded server. A
    hard limit already below the requested one is kept, since an
    unprivileged process cannot raise it. RLIMIT_NPROC counts every process
    of the user, not only this tree, so it is stricter on a busy account.
    """
    module = _resource if resource_module is None else resource_module
    if module is None:
        raise OSError("resource limits are not available on this platform")
    plan = []
    for name, value in (
        ("RLIMIT_NPROC", limits.process_limit),
        ("RLIMIT_AS", limits.job_memory_bytes),
        ("RLIMIT_FSIZE", limits.file_size_bytes),
    ):
        which = getattr(module, name, None)
        if which is None:
            raise OSError("%s is not available on this platform" % name)
        plan.append((which, value))
    infinity = module.RLIM_INFINITY
    getrlimit, setrlimit = module.getrlimit, module.setrlimit

    def apply_limits():
        for which, value in plan:
            _soft, hard = getrlimit(which)
            if hard != infinity:
                value = min(value, hard)
            setrlimit(which, (value, value))

    return apply_limits


def _run_process(cmd, cwd, stdin, timeout, language, limits=None):
    if language == "bash" and not sonder_paths.bash_executable():
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES[language]["missing"])
    # Encode before anything is spawned: a failure here (an unpaired surrogate
    # cannot be utf-8) used to escape after the child and its job existed,
    # leaving both live.
    try:
        input_bytes = (stdin or "").encode("utf-8")
    except UnicodeEncodeError as exc:
        return _error_result(
            language, cwd, timeout, "stdin is not encodable as UTF-8: %s" % exc,
        )
    try:
        popen_kwargs = {
            "cwd": cwd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            # Binary pipes: this lane runs several languages with different
            # output encodings, so the decode is done per-payload by
            # _decode_child_output rather than pinned to one codec here.
            "text": False,
            "env": _child_environment(),
            # Model-authored code must not inherit ambient server handles.
            "close_fds": True,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            if limits is not None:
                # Nothing runs until the job and its limits are in place.
                popen_kwargs["creationflags"] |= _CREATE_SUSPENDED
        else:
            popen_kwargs["start_new_session"] = True
            if limits is not None:
                popen_kwargs["preexec_fn"] = _posix_limit_preexec(limits)
        proc = subprocess.Popen(
            cmd,
            **popen_kwargs,
        )
    except FileNotFoundError:
        missing = SUPPORTED_LANGUAGES.get(language, {}).get(
            "missing", "executable not found: %s" % (cmd[0] if cmd else "(empty)")
        )
        return _error_result(language, cwd, timeout, missing)
    except (OSError, subprocess.SubprocessError) as exc:
        if limits is None:
            raise
        # Missing rlimit support, or the child's preexec_fn failed to set
        # them: refuse rather than run without the limits.
        return _error_result(
            language, cwd, timeout, "could not apply run limits; refusing to run: %s" % exc,
        )
    collect = {}
    if limits is not None:
        collect = {
            "capture_bytes": limits.capture_bytes_per_stream,
            "kill_after_bytes": limits.kill_after_total_bytes,
        }
    # Everything past the spawn owns a live child: any exception (including
    # KeyboardInterrupt) tears the tree down and reaps it before propagating,
    # and the job is closed exactly once on every path.
    try:
        proc._sonder_job_handle = None
        if limits is not None and os.name == "nt":
            proc._sonder_job_handle = _limited_windows_job(proc, limits)
            if not proc._sonder_job_handle:
                _abort_process(proc)
                return _error_result(
                    language, cwd, timeout,
                    "could not apply run limits (Windows job object); refusing to run",
                )
            _resume_windows_process(proc)
        else:
            proc._sonder_job_handle = _attach_windows_job(proc)
        stdout, stderr, status = _collect_output(proc, input_bytes, timeout, **collect)
    except BaseException:
        _abort_process(proc)
        raise
    finally:
        if os.name != "nt" and isinstance(proc, _REAL_POPEN):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        _release_job(proc)
    if status == "output":
        return {
            **_error_result(
                language, cwd, timeout,
                "output exceeded %d bytes; process killed" % limits.kill_after_total_bytes,
            ),
            "stdout": _trim_output(stdout),
            "stderr": _trim_output(stderr),
        }
    if status == "timeout":
        timeout_exc = subprocess.TimeoutExpired(
            cmd, timeout,
            output=_decode_child_output(stdout),
            stderr=_decode_child_output(stderr),
        )
        return _timeout_result(language, cwd, timeout, timeout_exc)
    completed = subprocess.CompletedProcess(
        cmd, proc.returncode,
        _decode_child_output(stdout), _decode_child_output(stderr),
    )
    return _completed_result(completed, language, cwd, timeout)


class _WindowRegistry:
    """Detached /runwindow consoles this runtime launched and still owns.

    Each console's process tree sits in a Job Object (kill-on-close, bounded
    process count) and has a lifetime timer; when the timer fires the tree
    is torn down.  A console the user closed is reaped on the next launch,
    which closes its job and so ends anything it left running.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._windows = {}

    def reap(self):
        with self._lock:
            self._reap_locked()

    def _reap_locked(self):
        for key, window in list(self._windows.items()):
            poll = getattr(window["proc"], "poll", None)
            if poll is not None and poll() is not None:
                self._release_locked(key)

    def _release_locked(self, key):
        window = self._windows.pop(key, None)
        if window is None:
            return
        window["timer"].cancel()
        _close_windows_job(window["job"])
        try:
            window["proc"]._sonder_job_handle = None
        except AttributeError:
            pass

    def live_count(self):
        with self._lock:
            self._reap_locked()
            return len(self._windows)

    def live_dirs(self):
        with self._lock:
            return {os.path.realpath(w["run_dir"]) for w in self._windows.values()}

    def launch(self, start, run_dir):
        """Start one console via ``start()`` if the live quota allows it."""
        with self._lock:
            self._reap_locked()
            if len(self._windows) >= RUN_WINDOW_MAX_LIVE:
                return None
            proc = start()
            job = _attach_windows_job(proc, active_process_limit=RUN_WINDOW_MAX_PROCESSES)
            if not job:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    logging.getLogger(__name__).warning(
                        "could not terminate uncontained runwindow console", exc_info=True)
                raise OSError("could not attach runwindow console to a bounded Windows job")
            try:
                _resume_windows_process(proc)
            except Exception as exc:  # noqa: BLE001 - never leave a failed resume registered
                _close_windows_job(job)
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                raise OSError("could not resume contained runwindow console") from exc
            try:
                proc._sonder_job_handle = job
            except AttributeError:
                pass
            key = id(proc)
            timer = threading.Timer(
                RUN_WINDOW_LIFETIME_SECONDS, self._expire, args=(key,))
            timer.daemon = True
            self._windows[key] = {
                "proc": proc, "job": job, "timer": timer, "run_dir": run_dir,
            }
            timer.start()
            return proc

    def _expire(self, key):
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                return
            try:
                _terminate_process_tree(window["proc"])
            except Exception:  # noqa: BLE001 - teardown is best effort; the job still closes
                logging.getLogger(__name__).warning(
                    "runwindow lifetime teardown failed", exc_info=True)
            # The teardown closes the job it terminated and clears the proc's
            # handle. Closing window["job"] again in the release would hit a
            # handle value Windows may already have reused (another console's
            # job, or any handle in this process), so drop it here.
            if getattr(window["proc"], "_sonder_job_handle", window["job"]) is None:
                window["job"] = None
            try:
                self._release_locked(key)
            except Exception:  # noqa: BLE001 - never kill the timer thread
                logging.getLogger(__name__).warning(
                    "runwindow lifetime release failed", exc_info=True)

    def shutdown(self):
        """Cancel every lifetime timer and release every owned console."""
        with self._lock:
            for key in list(self._windows):
                self._release_locked(key)


_WINDOWS = _WindowRegistry()


def _run_window_base():
    base = os.environ.get(RUN_WINDOW_DIR_ENV, "").strip()
    if not base:
        home = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        base = os.path.join(home, "sonder", "runs")
    return base


def _prune_run_dirs(base, keep):
    """Delete the oldest generated run dirs so at most ``keep`` remain.

    Only ``sonder-window-*`` directories are considered, never symlinks,
    and never the run directory of a console this runtime still owns.
    Returns how many generated run directories remain afterwards.
    """
    live = _WINDOWS.live_dirs()
    entries = []
    try:
        with os.scandir(base) as listing:
            for entry in listing:
                if not entry.name.startswith(_RUN_WINDOW_PREFIX):
                    continue
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    entries.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.path))
                except OSError:
                    continue
    except OSError:
        return 0
    entries.sort()
    remaining = len(entries)
    for _mtime, path in entries:
        if remaining <= keep:
            break
        if os.path.realpath(path) in live:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            remaining -= 1
    return remaining


def _persistent_run_dir():
    base = _run_window_base()
    os.makedirs(base, exist_ok=True)
    # Leave room for the directory about to be created.
    if _prune_run_dirs(base, RUN_WINDOW_MAX_RETAINED_DIRS - 1) >= RUN_WINDOW_MAX_RETAINED_DIRS:
        raise _RunWindowQuota(
            "%d /runwindow run directories under %s are still in use; close "
            "open consoles or remove old run directories"
            % (RUN_WINDOW_MAX_RETAINED_DIRS, base)
        )
    return tempfile.mkdtemp(prefix=_RUN_WINDOW_PREFIX, dir=base)


class _RunWindowQuota(RuntimeError):
    """A /runwindow lifecycle quota refused a new console."""


def _live_quota_message():
    return (
        "%d /runwindow consoles are already open (limit %d); close one before "
        "launching another" % (RUN_WINDOW_MAX_LIVE, RUN_WINDOW_MAX_LIVE)
    )


def _bat_quote(value):
    return '"%s"' % str(value).replace('"', "")


def _write_launcher(run_dir, lines):
    path = os.path.join(run_dir, "launch.bat")
    with open(path, "w", encoding="utf-8") as f:
        f.write("@echo off\r\n")
        f.write("cd /d %s\r\n" % _bat_quote(run_dir))
        for line in lines:
            f.write(line.rstrip() + "\r\n")
    return path


def _launch_console(launcher, cwd, language, timeout):
    if os.name != "nt":
        return _error_result(
            language,
            cwd,
            timeout,
            "/runwindow is only available on Windows consoles",
        )
    def start():
        return subprocess.Popen(
            ["cmd", "/k", launcher],
            cwd=cwd,
            creationflags=(getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                           | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)),
            env=_child_environment(),
            # A detached window is still executing model-authored code.  It
            # has no legitimate reason to inherit the host's other handles.
            close_fds=True,
        )

    try:
        proc = _WINDOWS.launch(start, os.path.dirname(launcher) or cwd)
    except FileNotFoundError:
        return _error_result(language, cwd, timeout, "cmd.exe not found")
    except OSError as exc:
        return _error_result(language, cwd, timeout, str(exc))
    if proc is None:
        return _error_result(language, cwd, timeout, _live_quota_message())
    return {
        "ok": True,
        "returncode": None,
        "stdout": "launched in a separate console window (pid %s)" % proc.pid,
        "stderr": "",
        "language": language,
        "cwd": cwd,
        "timeout": timeout,
        "error": "",
        "detached": True,
        "pid": proc.pid,
        "run_dir": os.path.dirname(launcher),
        # ``timeout`` bounded compile/launch only; this bounds the console.
        "window_lifetime": RUN_WINDOW_LIFETIME_SECONDS,
    }


def _cpp_compiler():
    for exe in ("g++", "clang++", "cl"):
        path = shutil.which(exe)
        if path:
            return exe, path
    vcvars = _find_visual_studio_vcvars()
    if vcvars:
        return "msvc-vcvars", vcvars
    return None, None


def _find_visual_studio_vcvars():
    override = os.environ.get("SONDER_VCVARS64", "").strip()
    if override and os.path.isfile(override):
        return override

    vswhere = shutil.which("vswhere")
    if not vswhere:
        candidate = os.path.join(
            sonder_paths.windows_program_files(x86=True),
            "Microsoft Visual Studio",
            "Installer",
            "vswhere.exe",
        )
        if os.path.isfile(candidate):
            vswhere = candidate
    if vswhere:
        try:
            proc = subprocess.run(
                [
                    vswhere,
                    "-latest",
                    "-products",
                    "*",
                    "-requires",
                    "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    "-property",
                    "installationPath",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                # Same forced-utf-8 child as the model-code lane above; an
                # install path outside cp1252 must not be silently mangled.
                encoding="utf-8",
                errors="replace",
                timeout=5,
                env=_child_environment(),
            )
            install = (proc.stdout or "").strip().splitlines()
            if install:
                joiner = ntpath.join if "\\" in install[0] or ":" in install[0] else os.path.join
                vcvars = joiner(install[0], "VC", "Auxiliary", "Build", "vcvars64.bat")
                if os.path.isfile(vcvars):
                    return vcvars
        except (OSError, subprocess.SubprocessError):
            pass

    roots = [
        os.path.join(sonder_paths.windows_program_files(), "Microsoft Visual Studio", "*", "*"),
        os.path.join(sonder_paths.windows_program_files(x86=True), "Microsoft Visual Studio", "*", "*"),
    ]
    for root in roots:
        matches = sorted(
            glob.glob(os.path.join(root, "VC", "Auxiliary", "Build", "vcvars64.bat")),
            reverse=True,
        )
        for path in matches:
            if os.path.isfile(path):
                return path
    return None


def _msvc_compile_result(vcvars, sources, exe, cwd, timeout, language):
    bat = os.path.join(cwd, "sonder_build_msvc.bat")
    quoted_sources = " ".join('"%s"' % src for src in sources)
    with open(bat, "w", encoding="utf-8") as f:
        f.write(
            '@echo off\r\n'
            'call "%s" >nul\r\n'
            'cl /nologo /EHsc /std:c++17 /Fe:"%s" %s\r\n'
            % (vcvars, exe, quoted_sources)
        )
    return _run_process(["cmd", "/c", bat], cwd, "", timeout, language)


def _run_cpp(path, tmp, stdin, timeout, cwd):
    name, compiler = _cpp_compiler()
    if not compiler:
        return _error_result("cpp", cwd, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"])
    exe_name = "snippet.exe" if os.name == "nt" or name in ("msvc-vcvars", "cl") else "snippet"
    exe = os.path.join(tmp, exe_name)
    if name == "msvc-vcvars":
        compile_result = _msvc_compile_result(compiler, [path], exe, tmp, timeout, "cpp")
    elif name == "cl":
        compile_cmd = [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe, path]
        compile_result = _run_process(compile_cmd, tmp, "", timeout, "cpp")
    else:
        compile_cmd = [compiler, "-std=c++17", path, "-o", exe]
        compile_result = _run_process(compile_cmd, tmp, "", timeout, "cpp")
    if not compile_result.get("ok"):
        return compile_result
    run_result = _run_process([exe], cwd, stdin, timeout, "cpp")
    run_result["stdout"] = _trim_output(
        (compile_result.get("stdout") or "") + (("\n" + run_result["stdout"]) if run_result.get("stdout") else "")
    )
    run_result["stderr"] = _trim_output(
        (compile_result.get("stderr") or "") + (("\n" + run_result["stderr"]) if run_result.get("stderr") else "")
    )
    return run_result


def _compile_cpp_for_window(path, run_dir, timeout):
    name, compiler = _cpp_compiler()
    if not compiler:
        return _error_result("cpp", run_dir, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"]), ""
    exe = os.path.join(run_dir, "snippet.exe")
    if name == "msvc-vcvars":
        result = _msvc_compile_result(compiler, [path], exe, run_dir, timeout, "cpp")
    elif name == "cl":
        result = _run_process(
            [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe, path],
            run_dir,
            "",
            timeout,
            "cpp",
        )
    else:
        result = _run_process(
            [compiler, "-std=c++17", path, "-o", exe],
            run_dir,
            "",
            timeout,
            "cpp",
        )
    return result, exe


def _run_rust(path, tmp, stdin, timeout, cwd):
    rustc = shutil.which("rustc")
    if not rustc:
        return _error_result(
            "rust", cwd, timeout, SUPPORTED_LANGUAGES["rust"]["missing"]
        )
    exe = os.path.join(tmp, "snippet.exe" if os.name == "nt" else "snippet.bin")
    compile_result = _run_process(
        [rustc, "-O", "--edition", "2021", path, "-o", exe],
        tmp, "", timeout, "rust",
    )
    if not compile_result["ok"]:
        compile_result["error"] = compile_result.get("error") or "rust compilation failed"
        return compile_result
    return _run_process([exe], cwd, stdin, timeout, "rust")


def _run_csharp(path, tmp, stdin, timeout, cwd):
    csc = shutil.which("csc")
    if csc:
        exe = os.path.join(tmp, "snippet.exe")
        compile_result = _run_process([csc, "/nologo", "/out:" + exe, path], tmp, "", timeout, "csharp")
        if not compile_result.get("ok"):
            return compile_result
        return _run_process([exe], cwd, stdin, timeout, "csharp")
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return _error_result("csharp", cwd, timeout, SUPPORTED_LANGUAGES["csharp"]["missing"])
    project = os.path.join(tmp, "Snippet.csproj")
    with open(project, "w", encoding="utf-8") as f:
        f.write(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <OutputType>Exe</OutputType>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '    <Nullable>enable</Nullable>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        )
    os.replace(path, os.path.join(tmp, "Program.cs"))
    return _run_process([dotnet, "run", "--project", project], cwd, stdin, timeout, "csharp")


def _compile_csharp_for_window(path, run_dir, timeout):
    csc = shutil.which("csc")
    if csc:
        exe = os.path.join(run_dir, "snippet.exe")
        result = _run_process([csc, "/nologo", "/out:" + exe, path], run_dir, "", timeout, "csharp")
        return result, [_bat_quote(exe)]
    dotnet = shutil.which("dotnet")
    if not dotnet:
        return _error_result("csharp", run_dir, timeout, SUPPORTED_LANGUAGES["csharp"]["missing"]), []
    program = os.path.join(run_dir, "Program.cs")
    os.replace(path, program)
    project = os.path.join(run_dir, "Snippet.csproj")
    with open(project, "w", encoding="utf-8") as f:
        f.write(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            '  <PropertyGroup>\n'
            '    <OutputType>Exe</OutputType>\n'
            '    <TargetFramework>net8.0</TargetFramework>\n'
            '    <ImplicitUsings>enable</ImplicitUsings>\n'
            '    <Nullable>enable</Nullable>\n'
            '  </PropertyGroup>\n'
            '</Project>\n'
        )
    result = _run_process([dotnet, "build", project, "--nologo", "-v:q"], run_dir, "", timeout, "csharp")
    return result, [_bat_quote(dotnet), "run", "--project", _bat_quote(project), "--no-build"]


def _clamp_iterations(max_iterations):
    return workflow_loop.clamp_iterations(max_iterations)


def _clamp_delay(delay_seconds):
    return workflow_loop.clamp_delay(delay_seconds)


def run_code(
    code, language="python", stdin="", timeout=DEFAULT_TIMEOUT, cwd=None, *,
    limits=None,
):
    """Run a snippet and return a result dict.

    ``limits`` (a RunLimits) opts into the hardened host run used by the
    chat run_code lane; None keeps the existing behaviour.

    The caller receives:
      ok: process exited 0
      returncode: int or None on timeout/unavailable
      stdout/stderr: trimmed process streams
      language/cwd/timeout: resolved execution metadata
      error: runner-level error text, when applicable
    """
    if not (code or "").strip():
        raise ValueError("code is empty")
    language = normalize_language(language)
    timeout = _clamp_timeout(timeout)
    cfg = SUPPORTED_LANGUAGES[language]
    if limits is not None and not isinstance(limits, RunLimits):
        raise TypeError("limits must be a RunLimits")
    if limits is not None and language in ("cpp", "csharp", "rust"):
        # Their compile steps and launchers are not wired for limits; refuse
        # rather than run a "limited" snippet without them.
        raise ValueError("run limits are not supported for %s" % language)

    # Windows can retain a directory handle briefly after taskkill tears down
    # a timed-out descendant tree.  Cleanup must not turn a correctly reported
    # timeout into a second exception; the bounded process teardown remains
    # authoritative and orphaned temp paths are disposable.
    with tempfile.TemporaryDirectory(
        prefix="sonder-run-", ignore_cleanup_errors=(os.name == "nt")
    ) as tmp:
        # The ungated default previously executed beside the runtime's own
        # control-plane modules, making a relative write a live self-modification.
        cwd = resolve_cwd(cwd) if cwd else os.path.realpath(tmp)
        path = os.path.join(tmp, "snippet" + cfg["suffix"])
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        if language == "cpp":
            return _run_cpp(path, tmp, stdin, timeout, cwd)
        if language == "csharp":
            return _run_csharp(path, tmp, stdin, timeout, cwd)
        if language == "rust":
            return _run_rust(path, tmp, stdin, timeout, cwd)
        cmd = cfg["cmd"](path)
        if limits is None:
            return _run_process(cmd, cwd, stdin, timeout, language)
        if limits.isolated_python and language == "python":
            # -I implies -E (ignore PYTHONIOENCODING), which drops Windows
            # children back to cp1252; -X utf8 keeps UTF-8 I/O enabled.
            cmd = [cmd[0], "-I", "-X", "utf8", *cmd[1:]]
        return _run_process(cmd, cwd, stdin, timeout, language, limits=limits)


def run_code_window(code, language="python", timeout=DEFAULT_TIMEOUT, cwd=None):
    """Compile/write a snippet and launch it in a separate Windows console.

    Unlike run_code(), this deliberately keeps the generated run directory so an
    interactive console app or compiled executable stays available after launch.
    ``timeout`` bounds compilation and launch only.  The console itself is
    bounded by the /runwindow lifecycle quota: at most ``RUN_WINDOW_MAX_LIVE``
    open at once, each torn down (with every process it started) after
    ``RUN_WINDOW_LIFETIME_SECONDS``, its process tree capped by a Job Object,
    and only the newest ``RUN_WINDOW_MAX_RETAINED_DIRS`` run dirs retained.
    """
    if not (code or "").strip():
        raise ValueError("code is empty")
    language = normalize_language(language)
    timeout = _clamp_timeout(timeout)
    if os.name != "nt":
        cwd = resolve_cwd(cwd) if cwd else os.path.realpath(tempfile.gettempdir())
        return _error_result(language, cwd, timeout, "/runwindow is only available on Windows consoles")

    cfg = SUPPORTED_LANGUAGES[language]
    if _WINDOWS.live_count() >= RUN_WINDOW_MAX_LIVE:
        fallback_cwd = resolve_cwd(cwd) if cwd else os.path.realpath(_run_window_base())
        return _error_result(language, fallback_cwd, timeout, _live_quota_message())
    try:
        run_dir = _persistent_run_dir()
    except _RunWindowQuota as exc:
        return _error_result(language, _run_window_base(), timeout, str(exc))
    cwd = resolve_cwd(cwd) if cwd else os.path.realpath(run_dir)
    path = os.path.join(run_dir, "snippet" + cfg["suffix"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    if language == "cpp":
        compile_result, exe = _compile_cpp_for_window(path, run_dir, timeout)
        if not compile_result.get("ok"):
            compile_result["run_dir"] = run_dir
            return compile_result
        launcher = _write_launcher(run_dir, [_bat_quote(exe)])
        return _launch_console(launcher, run_dir, language, timeout)

    if language == "csharp":
        compile_result, command = _compile_csharp_for_window(path, run_dir, timeout)
        if not compile_result.get("ok"):
            compile_result["run_dir"] = run_dir
            return compile_result
        launcher = _write_launcher(run_dir, [" ".join(command)])
        return _launch_console(launcher, run_dir, language, timeout)

    if language == "javascript" and not shutil.which("node"):
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["javascript"]["missing"])
    if language == "powershell" and not (shutil.which("pwsh") or shutil.which("powershell")):
        return _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["powershell"]["missing"])

    cmd = cfg["cmd"](path)
    launcher = _write_launcher(run_dir, [" ".join(_bat_quote(part) for part in cmd)])
    return _launch_console(launcher, run_dir, language, timeout)


def format_result(result):
    status = "ok" if result.get("ok") else "failed"
    rc = result.get("returncode")
    lines = [
        "status: %s" % status,
        "language: %s" % result.get("language"),
        "cwd: %s" % result.get("cwd"),
        "timeout: %ss" % result.get("timeout"),
        "returncode: %s" % ("(none)" if rc is None else rc),
    ]
    if result.get("error"):
        lines.append("error: %s" % result["error"])
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if "EOFError" in stderr:
        lines.append(
            "note: this program tried to read keyboard input, but /run is non-interactive."
        )
    if (
        result.get("error", "").startswith("timed out")
        and result.get("language") in ("csharp", "cpp", "project")
        and stdout
        and ("enter" in stdout.lower() or "guess" in stdout.lower() or "input" in stdout.lower())
    ):
        lines.append(
            "note: the program appears to be waiting for console input. For /run, add a scripted demo path or provide stdin."
        )
    if result.get("error", "").startswith("timed out"):
        lines.append("note: use a bounded smoke test or auto-exit path for /run.")
    if stdout:
        lines.extend(["", "stdout:", stdout])
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not stdout and not stderr and not result.get("error"):
        lines.append("")
        lines.append("(no output)")
    return "\n".join(lines)


def format_window_result(result):
    lines = [format_result(result)]
    if result.get("detached"):
        lines.append("run dir: %s" % result.get("run_dir"))
        lines.append("note: close the launched console window when you are done.")
        if result.get("window_lifetime"):
            lines.append(
                "note: the timeout covered compile/launch only; the console and "
                "every process it started are closed after %ss."
                % result["window_lifetime"]
            )
    elif result.get("run_dir"):
        lines.append("run dir: %s" % result.get("run_dir"))
    return "\n".join(line for line in lines if line)


def _project_files_from_json(files_json):
    try:
        parsed = json.loads(files_json) if isinstance(files_json, str) else files_json
    except json.JSONDecodeError as exc:
        raise ValueError("files_json is not valid JSON: %s" % exc)
    if isinstance(parsed, dict) and "files" in parsed:
        parsed = parsed["files"]
    if isinstance(parsed, dict):
        files = [{"path": path, "content": content} for path, content in parsed.items()]
    elif isinstance(parsed, list):
        files = parsed
    else:
        raise ValueError("files_json must be a dict of path->content or a list of file objects")
    if not files:
        raise ValueError("project has no files")
    if len(files) > MAX_PROJECT_FILES:
        raise ValueError("too many project files (max %d)" % MAX_PROJECT_FILES)
    total = 0
    clean = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("each project file must be an object")
        path = str(item.get("path") or "").replace("\\", "/").strip()
        content = item.get("content")
        if not path or content is None:
            raise ValueError("each project file needs path and content")
        if os.path.isabs(path) or path.startswith("../") or "/../" in path or path == "..":
            raise ValueError("unsafe project path: %r" % path)
        total += len(str(content).encode("utf-8"))
        if total > MAX_PROJECT_BYTES:
            raise ValueError("project content too large (max %d bytes)" % MAX_PROJECT_BYTES)
        clean.append({"path": path, "content": str(content)})
    return clean


def _write_project_files(root, files):
    for item in files:
        dest = os.path.abspath(os.path.join(root, item["path"]))
        try:
            inside = os.path.commonpath([root, dest]) == root
        except ValueError:
            inside = False
        if not inside:
            raise ValueError("unsafe project path: %r" % item["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(item["content"])


def _commands_from_json(commands_json):
    if not commands_json:
        return None
    try:
        parsed = json.loads(commands_json) if isinstance(commands_json, str) else commands_json
    except json.JSONDecodeError as exc:
        raise ValueError("commands_json is not valid JSON: %s" % exc)
    if isinstance(parsed, dict) and "commands" in parsed:
        parsed = parsed["commands"]
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("commands_json must be a non-empty list")
    if len(parsed) > MAX_PROJECT_COMMANDS:
        raise ValueError(
            "commands_json may list at most %d commands (got %d)"
            % (MAX_PROJECT_COMMANDS, len(parsed))
        )
    commands = []
    for item in parsed:
        if isinstance(item, list):
            cmd, cwd = item, ""
        elif isinstance(item, dict):
            cmd, cwd = item.get("cmd"), item.get("cwd", "")
        else:
            raise ValueError("each command must be an argv list or object")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
            raise ValueError("command cmd must be a non-empty argv string list")
        commands.append({"cmd": cmd, "cwd": str(cwd or "")})
    return commands


def _project_cwd(root, rel):
    path = os.path.abspath(os.path.join(root, rel or ""))
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:
        inside = False
    if not inside or not os.path.isdir(path):
        raise ValueError("command cwd must stay inside project: %r" % rel)
    return path


def _auto_project_commands(root, files):
    paths = [f["path"] for f in files]
    lower = [p.lower() for p in paths]
    py_main = next((p for p in paths if p.lower() in ("main.py", "app.py")), None)
    if py_main:
        return [{"cmd": [sys.executable, py_main], "cwd": ""}]
    csproj = next((p for p in paths if p.lower().endswith(".csproj")), None)
    if csproj:
        return [{"cmd": ["dotnet", "run", "--project", csproj], "cwd": ""}]
    cs_files = [p for p in paths if p.lower().endswith(".cs")]
    if cs_files:
        project = os.path.join(root, "SonderGenerated.csproj")
        with open(project, "w", encoding="utf-8") as f:
            f.write(
                '<Project Sdk="Microsoft.NET.Sdk">\n'
                '  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n'
                '</Project>\n'
            )
        return [{"cmd": ["dotnet", "run", "--project", "SonderGenerated.csproj"], "cwd": ""}]
    cpp_files = [p for p in paths if p.lower().endswith((".cpp", ".cc", ".cxx"))]
    if cpp_files:
        name, compiler = _cpp_compiler()
        if not compiler:
            return [{"cmd": ["__missing_cpp_compiler__"], "cwd": ""}]
        exe = os.path.abspath(os.path.join(root, "app.exe" if os.name == "nt" else "app"))
        if name == "msvc-vcvars":
            sources = [os.path.abspath(os.path.join(root, p)) for p in cpp_files]
            bat = os.path.join(root, "sonder_build_msvc.bat")
            quoted_sources = " ".join('"%s"' % src for src in sources)
            with open(bat, "w", encoding="utf-8") as f:
                f.write(
                    '@echo off\r\n'
                    'call "%s" >nul\r\n'
                    'cl /nologo /EHsc /std:c++17 /Fe:"%s" %s\r\n'
                    % (compiler, exe, quoted_sources)
                )
            compile_cmd = ["cmd", "/c", bat]
        elif name == "cl":
            compile_cmd = [compiler, "/nologo", "/EHsc", "/std:c++17", "/Fe:" + exe] + cpp_files
        else:
            compile_cmd = [compiler, "-std=c++17"] + cpp_files + ["-o", exe]
        return [{"cmd": compile_cmd, "cwd": ""}, {"cmd": [exe], "cwd": ""}]
    if "package.json" in lower:
        return [{"cmd": ["npm", "test"], "cwd": ""}]
    raise ValueError("could not auto-detect how to run project; provide commands_json")


def run_project(files_json, commands_json="", stdin="", timeout=MAX_TIMEOUT):
    files = _project_files_from_json(files_json)
    commands = _commands_from_json(commands_json)
    timeout = _clamp_timeout(timeout)
    with tempfile.TemporaryDirectory(prefix="sonder-project-") as root:
        root = os.path.abspath(root)
        _write_project_files(root, files)
        if commands is None:
            commands = _auto_project_commands(root, files)
        steps = []
        ok = True
        for index, command in enumerate(commands, start=1):
            cwd = _project_cwd(root, command.get("cwd", ""))
            cmd = command["cmd"]
            language = "project"
            if cmd and cmd[0] == "__missing_cpp_compiler__":
                result = _error_result(language, cwd, timeout, SUPPORTED_LANGUAGES["cpp"]["missing"])
            else:
                try:
                    result = _run_process(cmd, cwd, stdin if index == len(commands) else "", timeout, language)
                except ValueError as exc:
                    result = _error_result(language, cwd, timeout, str(exc))
            steps.append({"index": index, "cmd": cmd, "cwd": cwd, "result": result})
            if not result.get("ok"):
                ok = False
                break
    return {"ok": ok, "files": [f["path"] for f in files], "steps": steps, "timeout": timeout}


def format_project_result(result):
    lines = [
        "project status: %s" % ("ok" if result.get("ok") else "failed"),
        "files: %s" % ", ".join(result.get("files") or []),
        "timeout: %ss" % result.get("timeout"),
    ]
    for step in result.get("steps") or []:
        lines.append("")
        lines.append("step %d: %s" % (step["index"], " ".join(step.get("cmd") or [])))
        formatted = format_result(step["result"])
        for line in formatted.splitlines():
            lines.append("  " + line)
    return "\n".join(lines)


def run_loop(
    actions,
    dispatch_action,
    max_iterations=DEFAULT_LOOP_ITERATIONS,
    stop_on_failure=True,
    stop_on_success=False,
    delay_seconds=0,
    cancel_check=None,
):
    """Compatibility delegate to the package application use case."""
    return workflow_loop.run_loop(
        actions,
        dispatch_action,
        max_iterations=max_iterations,
        stop_on_failure=stop_on_failure,
        stop_on_success=stop_on_success,
        delay_seconds=delay_seconds,
        cancel_check=cancel_check,
    )


def format_loop_result(loop_result):
    return workflow_loop.format_loop_result(loop_result)
