"""Replay a recorded build script in headless Blender.

Model-written code never executes.  The host records the script into a JSON op list
(``domain/meshgen/dsl``); this module stages ``ops.json`` plus the trusted ``harness.py``,
``replay.py`` and ``opschema.py`` and runs Blender over them.  On Windows the run happens inside a
zero-capability AppContainer (``sandbox_win``), which bounds what even the trusted code can do with
hostile *data*; if the sandbox cannot be set up the build fails closed.
``SONDER_MESHGEN_UNSANDBOXED=1`` is the one explicit opt-out; results then carry
``sandbox: "unsandboxed"`` and a warning.

``build_candidate`` writes a JSON job file, runs ``blender -b --factory-startup --disable-autoexec
--python harness.py -- job.json`` (no shell, bounded by a timeout) and reads the harness' result
file, which is treated as untrusted input (``sanitize_result``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from . import sandbox_win

HARNESS_PATH = Path(__file__).with_name("harness.py")
REPLAY_PATH = Path(__file__).with_name("replay.py")
OPSCHEMA_PATH = Path(__file__).resolve().parents[2] / "domain" / "meshgen" / "dsl" / "opschema.py"
HARNESS_FILES = (HARNESS_PATH, REPLAY_PATH, OPSCHEMA_PATH)
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


def _posix_limit(process, limit_bytes: int) -> None:
    """Cap the child's address space after it started (Linux ``prlimit``); no ``preexec_fn``.

    ``preexec_fn`` runs Python code between fork and exec, which is unsafe in a threaded server.
    ``resource.prlimit`` is Linux-only, so elsewhere the cap is simply not applied (documented gap).
    """
    try:
        import resource
        resource.prlimit(process.pid, resource.RLIMIT_AS, (limit_bytes, limit_bytes))
    except (ImportError, AttributeError, OSError, ValueError):
        pass


def run_limited(argv, *, cwd, env, timeout, memory_mb=DEFAULT_MEMORY_MB):
    """``subprocess.run`` equivalent with a memory ceiling; returns ``(returncode, stdout, stderr)``.

    Windows: the child is placed in a Job Object (per-process committed-memory cap, killed with
    the job).  There is a short window between start and assignment; the cap is a backstop for
    runaway allocation, not a sandbox.  Linux: ``RLIMIT_AS`` via ``prlimit`` right after start.  Raises
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
    process = subprocess.Popen(argv, cwd=cwd, shell=False, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        if job is not None:
            job[0](process)
        elif os.name != "nt":
            _posix_limit(process, limit * 3)
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


MAX_RESULT_BYTES = 1024 * 1024          # the harness' result JSON
MAX_MODEL_BYTES = 256 * 1024 * 1024     # a copied-back GLB/FBX
MAX_IMAGE_BYTES = 64 * 1024 * 1024      # a copied-back render
MAX_TEXT_CHARS = 2000                   # any text that can reach a prompt or a payload
MAX_TAIL_LINE_CHARS = 300
_ALLOWED_RESULT_KEYS = frozenset({"ok", "error", "tris", "within_budget", "size", "files", "parts",
                                  "render_error", "tail", "traceback"})
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b[\w.-]*(?:api[_-]?key|access[_-]?key|secret|token|passw(?:or)?d|credential)[\w.-]*\s*[=:]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b(?:ghp|gho|ghs|github_pat|xox[abp]|AKIA|AIza)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b[A-Za-z0-9_-]{40,}={0,2}"),
)


def bound_text(value, limit: int = MAX_TEXT_CHARS) -> str:
    """Text for a prompt or payload: length capped, secret-looking runs removed (a free residual filter).

    No model code runs in Blender, so nothing script-controlled reaches here; this only bounds what the
    trusted harness and Blender themselves print.
    """
    text = str(value if value is not None else "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda match: "[redacted]", text)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def sanitize_result(result: dict) -> dict:
    """Keep only the keys the loop understands, with bounded, typed values.

    The result file is written inside the sandbox, so every field is treated as untrusted data.
    """
    clean: dict = {}
    for key in _ALLOWED_RESULT_KEYS & set(result):
        value = result[key]
        if key in ("error", "render_error"):
            clean[key] = bound_text(value)
        elif key in ("tail", "traceback"):
            lines = value if isinstance(value, list) else [value]
            clean[key] = [bound_text(line, MAX_TAIL_LINE_CHARS) for line in lines[-_TAIL_LINES:]]
        elif key == "tris":
            clean[key] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        elif key in ("ok", "within_budget"):
            clean[key] = bool(value)
        elif key == "size":
            seq = value if isinstance(value, list) else []
            clean[key] = [float(v) for v in seq[:3] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        elif key == "files":
            clean[key] = {str(k)[:20]: bound_text(v, 600)
                          for k, v in (value.items() if isinstance(value, dict) else [])}
        elif key == "parts":
            parts = []
            for part in (value if isinstance(value, list) else [])[:8]:
                if isinstance(part, dict):
                    tris = part.get("tris")
                    parts.append({"name": bound_text(part.get("name"), 40),
                                  "tris": int(tris) if isinstance(tris, (int, float)) and not isinstance(tris, bool) else 0})
            clean[key] = parts
    return clean


def _tail(text: str) -> list[str]:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    return [bound_text(line, MAX_TAIL_LINE_CHARS) for line in lines[-_TAIL_LINES:]]


def read_result(result_path: Path) -> dict | None:
    """The harness' result object, or ``None`` when it is missing, oversized or malformed."""
    try:
        if result_path.stat().st_size > MAX_RESULT_BYTES:
            return None
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return sanitize_result(value) if isinstance(value, dict) else None


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


def sandbox_mode(env=None) -> str:
    """``appcontainer`` or ``unsandboxed`` (explicit opt-out); raises ``SandboxUnavailable`` otherwise."""
    return sandbox_win.decide_mode(os.environ if env is None else env, os.name)


def preflight(blender) -> dict:
    """Verify the sandbox before any model time is spent; raises ``SandboxUnavailable`` (fail closed)."""
    mode = sandbox_mode()
    if mode == sandbox_win.SANDBOXED:
        info = {"sandbox": mode}
        info.update(sandbox_win.preflight(os.path.dirname(os.path.realpath(str(blender)))))
        return info
    reason = "explicit opt-out via %s" % sandbox_win.OPT_OUT_ENV
    return {"sandbox": mode, "warning": "Blender runs UNSANDBOXED (%s); no model-written code runs in it, but the "
            "trusted replay has this server's authority over the recorded data" % reason}


def _job_dict(ops_path, out_dir, name, kit_path, kit_allowed, max_tris, height_m, export, render, result_path):
    return {
        "ops_path": str(ops_path),
        "out_dir": str(out_dir),
        "name": name,
        "kit_path": str(kit_path) if kit_path else "",
        "kit_allowed": sorted(str(item) for item in (kit_allowed or ())),
        "max_tris": int(max_tris),
        "height_m": float(height_m or 0.0),
        "export": "fbx" if str(export).lower() == "fbx" else "glb",
        "render": bool(render),
        "result_path": str(result_path),
    }


def _copy_back(files: dict, destination: Path) -> dict:
    """Copy the build's files out of the run directory with per-file size caps; returns new paths."""
    moved = {}
    for key, value in files.items():
        source = Path(value)
        cap = MAX_MODEL_BYTES if source.suffix.lower() in (".glb", ".fbx") else MAX_IMAGE_BYTES
        size = source.stat().st_size
        if size > cap:
            raise ValueError("%s is %d MB, over the %d MB limit" % (key, size >> 20, cap >> 20))
        target = destination / source.name
        shutil.copyfile(source, target)
        moved[key] = str(target)
    return moved


def _stage_support(directory: Path) -> Path:
    """Copy the trusted harness files into ``directory``; returns the harness path."""
    for source in HARNESS_FILES:
        shutil.copyfile(source, directory / source.name)
    return directory / HARNESS_PATH.name


def _sandboxed(blender, ops_path, out, *, name, kit_path, kit_allowed, max_tris, height_m, export, render,
               timeout, memory_mb):
    blender = os.path.realpath(str(blender))
    blender_dir = os.path.dirname(blender)
    kit_file = Path(kit_path).resolve() if kit_path else None
    with sandbox_win.granted_run_dir(out, blender_dir, kit_file.parent if kit_file else None) as build:
        run = build.run
        harness = _stage_support(run)
        ops = run / "ops.json"
        shutil.copyfile(ops_path, ops)
        run_kit = None
        if kit_file:
            (run / "kit").mkdir()
            run_kit = run / "kit" / kit_file.name
            shutil.copyfile(kit_file, run_kit)
        run_out = run / "out"
        result_path = run / ("result-%s.json" % uuid.uuid4().hex)
        job = _job_dict(ops, run_out, name, run_kit, kit_allowed, max_tris, height_m, export, render, result_path)
        job_path = run / "job.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        argv = build_argv(blender, str(job_path), str(harness))
        env = sandbox_win.sandbox_env(run, blender_dir, os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or "")
        outcome = sandbox_win.run_sandboxed(argv, cwd=run, env=env, timeout=timeout, slot=build.slot,
                                            memory_mb=memory_mb)
        tail_text = outcome.stdout.decode("utf-8", "replace") + "\n" + outcome.stderr.decode("utf-8", "replace")
        if outcome.timed_out:
            result = {"ok": False, "error": "blender timed out after %ss" % timeout, "tail": _tail(tail_text)}
        else:
            result = read_result(result_path)
            failed = result is None or not result.get("ok")
            # Evidence for "memory limit": the job reached its limit AND the build failed in a way that fits
            # (no result at all, or the script/harness reported MemoryError).
            oom_marked = result is None or "MemoryError" in str(result.get("error", ""))
            if outcome.memory_exceeded and failed and oom_marked:
                result = {"ok": False, "error": "memory limit exceeded (%d MB)" % outcome.memory_limit_mb,
                          "tail": _tail(tail_text)}
            elif result is None:
                result = {"ok": False, "error": "no result; rc=%s" % outcome.returncode,
                          "tail": _tail(tail_text)}
            else:
                result = confine_files(result, run_out)
                if result.get("ok"):
                    used = sandbox_win.tree_size(run)
                    if used > sandbox_win.MAX_TREE_BYTES:
                        result = {"ok": False, "error": "the build wrote more than %d MB into its run directory"
                                  % (sandbox_win.MAX_TREE_BYTES >> 20)}
                    else:
                        try:
                            result = dict(result, files=_copy_back(result.get("files") or {}, out))
                        except (OSError, ValueError) as exc:
                            result = {"ok": False, "error": "cannot copy the build output back: %s" % exc}
    if build.problems:
        result = dict(result, warnings=[bound_text(item, 300) for item in build.problems[:3]])
    return result


def _unsandboxed(blender, ops_path, out, *, name, kit_path, kit_allowed, max_tris, height_m, export, render,
                 timeout, memory_mb):
    stage = out.resolve() / (".stage-%s" % uuid.uuid4().hex[:12])
    stage.mkdir()
    harness = _stage_support(stage)
    result_path = out.resolve() / ("%s.result-%s.json" % (name, uuid.uuid4().hex))
    job = _job_dict(Path(ops_path).resolve(), out.resolve(), name, Path(kit_path).resolve() if kit_path else None,
                    kit_allowed, max_tris, height_m, export, render, result_path)
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
        shutil.rmtree(stage, ignore_errors=True)
    stdout = raw_out.decode("utf-8", "replace")[-sandbox_win.OUTPUT_TAIL_BYTES:]
    stderr = raw_err.decode("utf-8", "replace")[-sandbox_win.OUTPUT_TAIL_BYTES:]
    result = read_result(result_path)
    try:
        result_path.unlink()
    except OSError:
        pass
    if result is None:
        return {"ok": False, "error": "no result; rc=%s" % returncode,
                "tail": _tail(stdout + "\n" + stderr)}
    return confine_files(result, out)


def build_candidate(blender, ops_path, out_dir, *, name, kit_path=None, kit_allowed=(), max_tris,
                    height_m, export="glb", render=True, timeout=180, memory_mb=None, sandbox=None) -> dict:
    """Replay one recorded candidate (``ops_path`` is its ``ops.json``) and build, export and render it.

    ``sandbox`` is ``None`` (decide from the environment) or an explicit mode.  ``memory_mb`` is the
    memory ceiling for this build; ``None`` means ``SONDER_MESHGEN_MEM_MB``/4096 when sandboxed and
    8192 on the unsandboxed path.  ``kit_allowed`` are the kit function names the ops may call.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    missing = [str(path) for path in HARNESS_FILES if not path.is_file()]
    if missing:
        return {"ok": False, "error": "harness files missing: %s" % ", ".join(missing)}
    common = dict(name=name, kit_path=kit_path, kit_allowed=kit_allowed, max_tris=max_tris, height_m=height_m,
                  export=export, render=render, timeout=timeout)
    mode = sandbox or sandbox_win.UNSANDBOXED
    try:
        mode = sandbox or sandbox_mode()
        if mode == sandbox_win.SANDBOXED:
            limit = int(memory_mb) if memory_mb else sandbox_win.memory_limit_mb(os.environ)
            result = _sandboxed(blender, ops_path, out, memory_mb=limit, **common)
        else:
            result = _unsandboxed(blender, ops_path, out, memory_mb=int(memory_mb or DEFAULT_MEMORY_MB), **common)
    except sandbox_win.SandboxUnavailable as exc:
        return {"ok": False, "error": "sandbox unavailable, build refused (not falling back to unsandboxed): %s"
                % bound_text(exc, 400), "sandbox": mode}
    result = dict(result, sandbox=mode)
    if mode != sandbox_win.SANDBOXED:
        result["warning"] = "built UNSANDBOXED"
    return result
