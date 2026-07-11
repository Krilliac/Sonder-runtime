"""grounding — sandboxed code execution for trilobite's self-learning loop.

Stdlib only. Pulls a fenced python code block out of a model response and
actually runs it (optionally with an appended assertion-based check) in a
subprocess, so pass/fail is grounded in real execution rather than a model's
own say-so.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_FILE_INFO_RE = re.compile(r"(?:^|\s)(?:file|path)\s*[:=]\s*([^\s`]+)", re.IGNORECASE)
_FILE_FIRST_LINE_RE = re.compile(
    r"^\s*(?://|#|<!--)\s*(?:file|path)\s*[:=]\s*([^\s<]+)\s*(?:-->)?\s*$",
    re.IGNORECASE,
)
DEFAULT_TIMEOUT = 8
MAX_TIMEOUT = 60
RUNNABLE_FENCE_LANGS = {
    "python": "python",
    "py": "python",
    "javascript": "javascript",
    "js": "javascript",
    "node": "javascript",
    "powershell": "powershell",
    "pwsh": "powershell",
    "ps1": "powershell",
    "cpp": "cpp",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "csharp": "csharp",
    "cs": "csharp",
    "c#": "csharp",
}


def normalize_language(language):
    lang = (language or "python").strip().lower()
    return RUNNABLE_FENCE_LANGS.get(lang, lang)


_LANG_FENCE = {
    "python": "python",
    "javascript": "javascript",
    "powershell": "powershell",
    "cpp": "cpp",
    "csharp": "csharp",
}


def extract_code_block(text, language=None):
    """Return the best runnable code block from a model response.

    By default this returns Python, preferring explicit python fences and
    ignoring bare shell-command blocks such as `/run python file.py`. Pass a
    language to select a runnable non-Python fence.
    """
    blocks = [
        ((lang or "").strip().lower(), body.strip())
        for lang, body in _CODE_BLOCK_RE.findall(text or "")
    ]
    if language is None:
        for lang, code in reversed(blocks):
            first = (lang.split() or [""])[0]
            if first and normalize_language(first) == "python":
                return code
        for lang, code in reversed(blocks):
            if lang == "" and not _looks_like_shell_block(code):
                return code
        return None
    want = normalize_language(language)
    for lang, code in reversed(blocks):
        first = (lang.split() or [""])[0]
        if normalize_language(first) == want:
            return code
    return None


def _fence_language(info):
    first = ((info or "").strip().split() or [""])[0].lower()
    return RUNNABLE_FENCE_LANGS.get(first)


def extract_runnable_code_block(text):
    """Return {"language": ..., "code": ...} for the best single runnable block.

    Unlike extract_code_block(), this accepts non-Python runnable fences for the
    user-facing /run command. Bare fences are treated as Python unless they look
    like shell commands.
    """
    blocks = [
        ((lang or "").strip(), body.strip())
        for lang, body in _CODE_BLOCK_RE.findall(text or "")
    ]
    for info, body in reversed(blocks):
        language = _fence_language(info)
        if language:
            return {"language": language, "code": body}
    for info, body in reversed(blocks):
        if not (info or "").strip() and not _looks_like_shell_block(body):
            return {"language": "python", "code": body}
    return None


def _path_from_fence(info, body):
    m = _FILE_INFO_RE.search(info or "")
    if m:
        return m.group(1), body
    lines = (body or "").splitlines()
    if lines:
        m = _FILE_FIRST_LINE_RE.match(lines[0])
        if m:
            return m.group(1), "\n".join(lines[1:]).lstrip("\n")
    return None, body


def extract_project_files(text):
    """Extract multi-file project blocks from Markdown fences.

    Supported forms:
      ```file:src/main.cpp
      ...
      ```

      ```cpp file=src/main.cpp
      ...
      ```

      ```cpp
      // file: src/main.cpp
      ...
      ```
    """
    files = []
    for info, body in _CODE_BLOCK_RE.findall(text or ""):
        path, content = _path_from_fence(info, body)
        if path:
            files.append({"path": path.strip(), "content": content.strip()})
    return files


def _looks_like_shell_block(body):
    lines = [line.strip() for line in (body or "").splitlines() if line.strip()]
    if not lines:
        return True
    first = lines[0].lower()
    shell_prefixes = (
        "/run ",
        "$ ",
        "> ",
        "python ",
        "python3 ",
        "py ",
        "pip ",
        "cd ",
        "bash ",
        "sh ",
        "powershell ",
        "pwsh ",
    )
    return first.startswith(shell_prefixes)


def _decode_timeout_stream(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def clamp_timeout(timeout, default=DEFAULT_TIMEOUT, maximum=MAX_TIMEOUT):
    try:
        value = int(timeout)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def run_code_detail(
    code,
    extra="",
    timeout=DEFAULT_TIMEOUT,
    interp=None,
    stdin="",
    compile_first=False,
):
    """Run code in a fresh subprocess and return structured execution details."""
    timeout = clamp_timeout(timeout)
    interp = interp or sys.executable
    src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        try:
            if compile_first:
                try:
                    compile(src, path, "exec")
                except (SyntaxError, ValueError, OverflowError) as exc:
                    return {
                        "ok": False,
                        "returncode": 1,
                        "stdout": "",
                        "stderr": "compile failed\n%s" % exc,
                        "timeout": timeout,
                        "timed_out": False,
                        "error": "",
                    }
            p = subprocess.run(
                [interp, path],
                input=stdin or "",
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "ok": p.returncode == 0,
                "returncode": p.returncode,
                "stdout": (p.stdout or "").strip(),
                "stderr": (p.stderr or "").strip(),
                "timeout": timeout,
                "timed_out": False,
                "error": "",
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "returncode": None,
                "stdout": _decode_timeout_stream(exc.stdout).strip(),
                "stderr": _decode_timeout_stream(exc.stderr).strip(),
                "timeout": timeout,
                "timed_out": True,
                "error": "timed out after %ss" % timeout,
            }
    finally:
        os.unlink(path)


def format_run_result(result):
    """Format a structured run result for humans using the REPL/HTTP /run command."""
    if result.get("timed_out"):
        status = "timed out"
    else:
        status = "ok" if result.get("ok") else "failed"
    rc = result.get("returncode")
    lines = [
        "status: %s" % status,
        "timeout: %ss" % result.get("timeout"),
        "returncode: %s" % ("(none)" if rc is None else rc),
    ]
    if result.get("error"):
        lines.append("error: %s" % result["error"])
    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    if "EOFError" in stderr:
        lines.append(
            "note: this program tried to read keyboard input, but /run is "
            "non-interactive. Generate a scripted smoke test or pass data another way."
        )
    if result.get("timed_out"):
        lines.append(
            "note: the process was still running. For /run, games and demos need "
            "a bounded smoke-test path or an auto-exit timer."
        )
    if stdout:
        lines.extend(["", "stdout:", stdout])
    if stderr:
        lines.extend(["", "stderr:", stderr])
    if not stdout and not stderr:
        lines.extend(["", "(no stdout/stderr captured)"])
    return "\n".join(lines)


def run_code(code, extra="", timeout=DEFAULT_TIMEOUT, interp=None, compile_first=True):
    """Run `code` (plus optional `extra` appended, e.g. assertions) in a fresh
    subprocess. Returns (ok, output) where ok is True iff the process exited 0,
    and output is combined stdout+stderr.
    """
    result = run_code_detail(
        code,
        extra=extra,
        timeout=timeout,
        interp=interp,
        compile_first=compile_first,
    )
    output = "\n".join(
        part for part in (
            result.get("stdout") or "",
            result.get("stderr") or "",
            result.get("error") or "",
        )
        if part
    ).strip()
    return result.get("ok") is True, output


def compile_code(code, timeout=8, interp=None):
    """Syntax-compile Python code without executing it."""
    interp = interp or sys.executable
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            c = subprocess.run(
                [interp, "-m", "py_compile", path],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if c.returncode == 0:
                return True, "compiled"
            return False, ("compile failed\n" + ((c.stdout or "") + (c.stderr or "")).strip()).strip()
        except subprocess.TimeoutExpired:
            return False, "(timed out after %ss)" % timeout
    finally:
        os.unlink(path)


def _combine(proc):
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _missing(exe):
    return False, "missing runtime/compiler: %s" % exe


def _run_cmd(cmd, timeout, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode == 0, _combine(p)
    except FileNotFoundError:
        return _missing(cmd[0])
    except subprocess.TimeoutExpired:
        return False, "(timed out after %ss)" % timeout


def _run_javascript(code, extra, timeout, execute):
    node = shutil.which("node")
    if not node:
        return _missing("node")
    src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        ok, out = _run_cmd([node, "--check", path], timeout)
        if not ok or not execute:
            return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
        return _run_cmd([node, path], timeout)
    finally:
        os.unlink(path)


def _run_powershell(code, extra, timeout, execute):
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return _missing("pwsh/powershell")
    src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        if not execute:
            return True, "compiled"
        return _run_cmd([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], timeout)
    finally:
        os.unlink(path)


def _run_cpp(code, extra, timeout, execute):
    compiler = shutil.which("g++") or shutil.which("clang++") or shutil.which("cl")
    if not compiler:
        return _missing("g++/clang++/cl")
    src = code + (("\n\n" + extra) if extra else "")
    with tempfile.TemporaryDirectory() as td:
        source = os.path.join(td, "main.cpp")
        exe = os.path.join(td, "main.exe" if os.name == "nt" else "main")
        with open(source, "w", encoding="utf-8") as f:
            f.write(src)
        if os.path.basename(compiler).lower() == "cl.exe":
            ok, out = _run_cmd([compiler, "/nologo", "/EHsc", source, "/Fe:" + exe], timeout, cwd=td)
        else:
            ok, out = _run_cmd([compiler, "-std=c++17", source, "-o", exe], timeout, cwd=td)
        if not ok or not execute:
            return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
        return _run_cmd([exe], timeout, cwd=td)


def _run_csharp(code, extra, timeout, execute):
    compiler = shutil.which("csc")
    dotnet = shutil.which("dotnet")
    src = code + (("\n\n" + extra) if extra else "")
    with tempfile.TemporaryDirectory() as td:
        source = os.path.join(td, "Program.cs")
        exe = os.path.join(td, "Program.exe")
        with open(source, "w", encoding="utf-8") as f:
            f.write(src)
        if compiler:
            ok, out = _run_cmd([compiler, "/nologo", "/out:" + exe, source], timeout, cwd=td)
            if not ok or not execute:
                return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
            return _run_cmd([exe], timeout, cwd=td)
        if dotnet:
            project = os.path.join(td, "App.csproj")
            with open(project, "w", encoding="utf-8") as f:
                f.write('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup></Project>')
            ok, out = _run_cmd([dotnet, "build", project, "--nologo", "-v:q"], timeout, cwd=td)
            if not ok or not execute:
                return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
            return _run_cmd([dotnet, "run", "--project", project, "--no-build"], timeout, cwd=td)
        return _missing("csc/dotnet")


def run_language_code(code, language="python", extra="", timeout=8, interp=None, execute=True):
    """Compile and optionally run code in a supported language."""
    lang = normalize_language(language)
    timeout = max(1, min(int(timeout or 8), 120))
    if lang == "python":
        if execute:
            return run_code(code, extra, timeout=timeout, interp=interp, compile_first=True)
        return compile_code(code, timeout=timeout, interp=interp)
    if lang == "javascript":
        return _run_javascript(code, extra, timeout, execute)
    if lang == "powershell":
        return _run_powershell(code, extra, timeout, execute)
    if lang == "cpp":
        return _run_cpp(code, extra, timeout, execute)
    if lang == "csharp":
        return _run_csharp(code, extra, timeout, execute)
    return False, "unsupported language: %s" % language


def _normalize_job(job, index, default_timeout):
    if isinstance(job, str):
        return {
            "name": "job-%d" % (index + 1),
            "language": "python",
            "code": job,
            "extra": "",
            "timeout": default_timeout,
            "execute": True,
        }
    if not isinstance(job, dict):
        raise ValueError("job %d must be a string or object" % (index + 1))
    code = job.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("job %d is missing non-empty code" % (index + 1))
    timeout = job.get("timeout", default_timeout)
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    timeout = max(1, min(timeout, 120))
    return {
        "name": str(job.get("name") or "job-%d" % (index + 1)),
        "language": normalize_language(job.get("language") or job.get("lang") or "python"),
        "code": code,
        "extra": str(job.get("extra") or job.get("check") or ""),
        "timeout": timeout,
        "execute": bool(job.get("execute", True)),
    }


def run_code_jobs(jobs, max_workers=4, default_timeout=8, interp=None):
    """Compile and run many snippets in parallel.

    jobs may be strings or dicts with code/name/language/extra/check/timeout/execute.
    Returns a list of result dicts in input order.
    """
    if not isinstance(jobs, list):
        raise ValueError("jobs must be a list")
    if not jobs:
        return []
    max_workers = max(1, min(int(max_workers or 1), 16, len(jobs)))
    default_timeout = max(1, min(int(default_timeout or 8), 120))
    normalized = [_normalize_job(job, i, default_timeout) for i, job in enumerate(jobs)]
    results = [None] * len(normalized)

    def one(index, job):
        started = time.time()
        ok, out = run_language_code(
            job["code"],
            language=job["language"],
            extra=job["extra"],
            timeout=job["timeout"],
            interp=interp,
            execute=job["execute"],
        )
        return {
            "index": index,
            "name": job["name"],
            "language": job["language"],
            "ok": bool(ok),
            "output": out,
            "seconds": round(time.time() - started, 3),
        }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(one, i, job) for i, job in enumerate(normalized)]
        for future in as_completed(futures):
            result = future.result()
            results[result["index"]] = result
    return results


def format_code_jobs(results):
    passed = sum(1 for r in results if r.get("ok"))
    lines = ["parallel code jobs: %d/%d passed" % (passed, len(results))]
    for r in results:
        status = "PASS" if r.get("ok") else "FAIL"
        lines.append("[%s] %s [%s] (%.3fs)" % (
            status,
            r.get("name", "?"),
            r.get("language", "python"),
            r.get("seconds", 0),
        ))
        out = (r.get("output") or "").strip()
        if out:
            lines.append(out[:2000])
    return "\n".join(lines)
