"""Run a model-written build script in headless Blender.

``build_candidate`` writes a JSON job file, runs ``blender -b --factory-startup
--disable-autoexec --python harness.py -- job.json`` (no shell, bounded by a
timeout, ``cwd`` the output directory) and parses the harness' final
``MESHGEN_RESULT {json}`` stdout line.  The harness is defence in depth, not a
sandbox: it runs with the same authority as ``run_code``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

HARNESS_PATH = Path(__file__).with_name("harness.py")
_TAIL_LINES = 8
DEFAULT_MEMORY_MB = 8192
SCRATCH_NAME = ".blender_profile"


# Variables copied from the parent environment into the Blender subprocess.  Everything else
# (API keys, tokens, SONDER_*, cloud credentials, ...) is dropped: model-written scripts run
# in that process, and any value in its environment is one reflection bug from a result string.
ENV_ALLOWLIST = frozenset({
    "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATHEXT", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS", "LANG", "LC_ALL",
})
# Profile and temp locations are redirected to a per-build scratch directory so Blender neither
# reads nor writes the real user profile.
_SCRATCH_VARIABLES = ("TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                      "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME")


def build_env(parent, scratch: Path, blender: str) -> dict:
    """Explicit allow-list environment for the Blender subprocess."""
    env = {}
    for key, value in parent.items():
        if key.upper() in ENV_ALLOWLIST:
            env[key.upper() if os.name == "nt" else key] = value
    system_root = env.get("SYSTEMROOT") or env.get("WINDIR") or ""
    if system_root:
        system_dirs = [os.path.join(system_root, "System32"), system_root]
    else:
        system_dirs = ["/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join([os.path.dirname(os.path.abspath(blender))] + system_dirs)
    scratch.mkdir(parents=True, exist_ok=True)
    for key in _SCRATCH_VARIABLES:
        target = scratch if key in ("TEMP", "TMP", "TMPDIR") else scratch / key.lower()
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    return env


def _windows_job(limit_bytes: int):
    """A Job Object that caps committed memory per process and dies with its handle, or ``None``."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", Counters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = Extended()
    info.Basic.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    info.ProcessMemoryLimit = int(limit_bytes)
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel.CloseHandle(handle)
        return None

    def assign(process) -> bool:
        return bool(kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))))

    def close():
        kernel.CloseHandle(handle)

    return assign, close


def _posix_limit(limit_bytes: int):
    """``preexec_fn`` capping the child's address space (virtual, so deliberately generous)."""
    def apply():
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    return apply


def run_limited(argv, *, cwd, env, timeout, memory_mb=DEFAULT_MEMORY_MB):
    """``subprocess.run`` equivalent with a memory ceiling; returns ``(returncode, stdout, stderr)``.

    Windows: the child is placed in a Job Object (per-process committed-memory cap, killed with
    the job).  There is a short window between start and assignment; the cap is a backstop for
    runaway allocation, not a sandbox.  POSIX: ``RLIMIT_AS`` is set before exec.  Raises
    ``subprocess.TimeoutExpired`` (carrying the partial stdout) on timeout.
    """
    limit = max(256, int(memory_mb)) * 1024 * 1024
    job = None
    kwargs = {}
    if os.name == "nt":
        try:
            job = _windows_job(limit)
        except Exception:  # noqa: BLE001 - the cap is best effort; the build still runs
            job = None
    else:
        kwargs["preexec_fn"] = _posix_limit(limit * 3)
    process = subprocess.Popen(argv, cwd=cwd, shell=False, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        if job is not None:
            job[0](process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
        return process.returncode, stdout, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if job is not None:
            job[1]()


def locate_blender(env=os.environ) -> str | None:
    """``SONDER_BLENDER`` when it names an existing file, else ``blender`` on PATH."""
    configured = str(env.get("SONDER_BLENDER", "") or "").strip().strip('"')
    if configured and os.path.isfile(configured):
        return configured
    return shutil.which("blender")


def build_argv(blender: str, job_path: str, harness: str = str(HARNESS_PATH)) -> list[str]:
    return [blender, "-b", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1",
            "--python", harness, "--", job_path]


def _tail(text: str) -> list[str]:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    return lines[-_TAIL_LINES:]


def read_result(result_path: Path) -> dict | None:
    """The harness' result object, or ``None`` when it is missing or malformed."""
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def confine_files(result: dict, out_dir: Path) -> dict:
    """Refuse a result whose ``files`` entries are not existing files inside ``out_dir``."""
    root = os.path.realpath(out_dir)
    for key, value in (result.get("files") or {}).items():
        real = os.path.realpath(str(value))
        try:
            inside = os.path.commonpath([root, real]) == root and real != root
        except ValueError:
            inside = False
        if not inside:
            return {"ok": False, "error": "result names a file outside the output directory: %s" % key}
        if not os.path.isfile(real):
            return {"ok": False, "error": "result names a file that does not exist: %s" % key}
    return result


def build_candidate(blender, code_path, out_dir, *, name, kit_path=None, kit_excluded=(), max_tris,
                    height_m, export="glb", render=True, timeout=180, memory_mb=DEFAULT_MEMORY_MB) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    harness = HARNESS_PATH
    if not harness.is_file():
        return {"ok": False, "error": "harness missing: %s" % harness}
    job = {
        "code_path": str(Path(code_path).resolve()),
        "out_dir": str(out.resolve()),
        "name": name,
        "kit_path": str(Path(kit_path).resolve()) if kit_path else "",
        "kit_excluded": sorted(str(item) for item in (kit_excluded or ())),
        "max_tris": int(max_tris),
        "height_m": float(height_m or 0.0),
        "export": "fbx" if str(export).lower() == "fbx" else "glb",
        "render": bool(render),
        "result_path": str(out.resolve() / ("%s.result-%s.json" % (name, uuid.uuid4().hex))),
    }
    result_path = Path(job["result_path"])
    job_path = out / ("%s.job.json" % name)
    job_path.write_text(json.dumps(job), encoding="utf-8")
    argv = build_argv(str(blender), str(job_path), str(harness))
    scratch = out / SCRATCH_NAME
    try:
        returncode, raw_out, raw_err = run_limited(
            argv, cwd=str(out), env=build_env(os.environ, scratch, str(blender)), timeout=timeout,
            memory_mb=memory_mb)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return {"ok": False, "error": "blender timed out after %ss" % timeout, "tail": _tail(partial)}
    except OSError as exc:
        return {"ok": False, "error": "could not start blender: %s: %s" % (type(exc).__name__, exc)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    stdout = raw_out.decode("utf-8", "replace")
    stderr = raw_err.decode("utf-8", "replace")
    result = read_result(result_path)
    try:
        result_path.unlink()
    except OSError:
        pass
    if result is None:
        return {"ok": False, "error": "no result; rc=%s" % returncode,
                "tail": _tail(stdout + "\n" + stderr)}
    return confine_files(result, out)
