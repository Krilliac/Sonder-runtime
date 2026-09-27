"""grounding — sandboxed code execution for Sonder Runtime's grounded-practice loop.

Stdlib only. Pulls a fenced python code block out of a model response and
actually runs it (optionally with an appended assertion-based check) in a
subprocess, so pass/fail is grounded in real execution rather than a model's
own say-so.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import sonder_logging

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
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "ruby": "ruby",
    "rb": "ruby",
    "perl": "perl",
    "pl": "perl",
    "php": "php",
    "lua": "lua",
    "r": "r",
    "rscript": "r",
    "go": "go",
    "golang": "go",
    "java": "java",
    "typescript": "typescript",
    "ts": "typescript",
    "rust": "rust",
    "rs": "rust",
}

# Formal proof fences are selectable by ``extract_code_block`` but are not
# executable through the general-purpose /run language surface. They go to the
# dedicated Lean verifier, which applies trust-gap checks and the pinned Lake
# environment before invoking the kernel.
EXTRACTION_FENCE_LANGS = {
    "lean": "lean",
    "lean4": "lean",
}


def normalize_language(language):
    lang = (language or "python").strip().lower()
    return RUNNABLE_FENCE_LANGS.get(
        lang, EXTRACTION_FENCE_LANGS.get(lang, lang),
    )


_LANG_FENCE = {
    "python": "python",
    "javascript": "javascript",
    "powershell": "powershell",
    "cpp": "cpp",
    "csharp": "csharp",
    "bash": "bash",
    "ruby": "ruby",
    "perl": "perl",
    "php": "php",
    "lua": "lua",
    "r": "r",
    "go": "go",
    "java": "java",
    "typescript": "typescript",
    "rust": "rust",
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


def _scratch_cwd():
    """A throwaway working directory for generated code.

    Generated code is untrusted model output. With no explicit cwd the child
    inherits this process's, which for the nightly job is the Sonder source
    tree, so a relative-path write lands in the repository. Not hypothetical:
    an overnight PowerShell candidate created a stray file named "2" in the
    repo root through a mis-typed redirect.
    """
    return tempfile.mkdtemp(prefix="sonder-gen-")


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
            # Generated code is untrusted model output: give it a throwaway
            # cwd so a relative-path write cannot land wherever this process
            # happens to be running (for the nightly job, the source tree).
            scratch = _scratch_cwd()
            try:
                p = subprocess.run(
                    [interp, path],
                    input=stdin or "",
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=scratch,
                    env=sonder_logging.child_environment(),
                )
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
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
    bytecode_path = path + "c"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)
        try:
            c = subprocess.run(
                [
                    interp,
                    "-c",
                    (
                        "import py_compile,sys; "
                        "py_compile.compile(sys.argv[1], cfile=sys.argv[2], "
                        "doraise=True)"
                    ),
                    path,
                    bytecode_path,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=sonder_logging.child_environment(),
            )
            if c.returncode == 0:
                return True, "compiled"
            return False, ("compile failed\n" + ((c.stdout or "") + (c.stderr or "")).strip()).strip()
        except subprocess.TimeoutExpired:
            return False, "(timed out after %ss)" % timeout
    finally:
        # py_compile otherwise leaves a unique .pyc in the shared temp cache.
        for temporary_path in (path, bytecode_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _combine(proc):
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def _missing(exe):
    return False, "missing runtime/compiler: %s" % exe


def _run_cmd(cmd, timeout, cwd=None):
    disposable = _scratch_cwd() if cwd is None else ""
    try:
        p = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, cwd=cwd or disposable,
            env=sonder_logging.child_environment(),
        )
        return p.returncode == 0, _combine(p)
    except FileNotFoundError:
        return _missing(cmd[0])
    except subprocess.TimeoutExpired:
        return False, "(timed out after %ss)" % timeout
    finally:
        if disposable:
            shutil.rmtree(disposable, ignore_errors=True)


# Appended checks run after the candidate code, so code that terminates the
# process successfully first (process.exit(0), PowerShell ``exit 0``,
# std::exit(0), Environment.Exit(0)) would pass without one check running.
# As in ``run_code_detail`` for Python, a per-run random sentinel is emitted
# only after the checks finish; a run without it fails, and the sentinel is
# stripped from the reported output.
_CHECKS_NOT_FINISHED = (
    "the program exited before the appended checks finished; they did not run"
)


def _checks_sentinel():
    return "__SONDER_CHECKS_DONE_%s__" % secrets.token_hex(16)


def _sentinel_literal(sentinel):
    # A double-quoted newline-sentinel-newline literal, valid in JS, C++ and
    # C#; the sentinel itself is ASCII letters, digits and underscores only.
    return '"\\n%s\\n"' % sentinel


def _finish_checked_run(result, sentinel):
    ok, out = result
    if not sentinel:
        return ok, out
    finished = sentinel in out
    out = out.replace("\n" + sentinel + "\n", "").replace(sentinel, "").strip()
    if not finished:
        out = (out + "\n" + _CHECKS_NOT_FINISHED).strip()
    return ok and finished, out


def _javascript_checked_source(code, extra, sentinel):
    # fs.writeSync is synchronous, so the sentinel cannot be lost to a
    # buffered stream or reordered after a later process.exit().
    return "%s\n\n%s\n;require('fs').writeSync(1, %s);\n" % (
        code, extra, _sentinel_literal(sentinel),
    )


def _powershell_checked_source(code, extra, sentinel):
    # A final check statement that failed without terminating (Write-Error,
    # a failing native command) leaves $? false; capture it before the
    # sentinel write and fail the run instead of letting it read as a pass.
    return (
        "%s\n\n%s\n"
        "$__sonder_checks_ok = $?\n"
        "[Console]::Out.Write(\"`n%s`n\"); [Console]::Out.Flush()\n"
        "if (-not $__sonder_checks_ok) { exit 1 }\n"
    ) % (code, extra, sentinel)


_CPP_MAIN_RE = re.compile(r"\bint\s+main\s*\(([^)]*)\)\s*\{")
_CSHARP_MAIN_RE = re.compile(r"\bstatic\s+(?:async\s+)?[\w.<>]+\s+(Main)\s*\(")


def _matching_brace(source, open_index):
    """Index of the ``}`` closing the ``{`` at ``open_index`` (C-like lexing)."""
    depth, index, length = 0, open_index, len(source)
    while index < length:
        char = source[index]
        pair = source[index:index + 2]
        if pair == "//":
            index = source.find("\n", index)
            if index < 0:
                return -1
            continue
        if pair == "/*":
            index = source.find("*/", index + 2)
            if index < 0:
                return -1
            index += 2
            continue
        if char in "\"'":
            index += 1
            while index < length and source[index] != char:
                index += 2 if source[index] == "\\" else 1
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _cpp_checked_source(code, extra, sentinel):
    """Run the checks' ``main`` as a function, then emit the sentinel.

    Returns ``None`` when the checks define no ``int main(...)`` (nothing to
    instrument).  ``return 0;`` is added at the end of the renamed body
    because falling off a non-``main`` function is undefined.
    """
    match = _CPP_MAIN_RE.search(extra)
    if not match:
        return None
    close = _matching_brace(extra, match.end() - 1)
    if close < 0:
        return None
    entry = "__sonder_checks_main_%s" % secrets.token_hex(8)
    params = match.group(1).strip()
    takes_args = bool(params) and params != "void"
    renamed = (
        extra[:match.start()] + "int %s(%s) {" % (entry, match.group(1))
        + extra[match.end():close] + "\nreturn 0;\n" + extra[close:]
    )
    wrapper = (
        "\n#include <cstdio>\n"
        "int main(int argc, char** argv) {\n"
        "    (void)argc; (void)argv;\n"
        "    int sonder_checks_rc = %s(%s);\n"
        "    std::fputs(%s, stdout);\n"
        "    std::fflush(stdout);\n"
        "    return sonder_checks_rc;\n"
        "}\n"
    ) % (entry, "argc, argv" if takes_args else "", _sentinel_literal(sentinel))
    return "%s\n\n%s\n%s" % (code, renamed, wrapper)


def _csharp_checked_source(code, extra, sentinel):
    """Invoke the checks' ``Main`` through a wrapper entry point.

    Returns ``None`` when the checks define no ``static ... Main(`` method.
    """
    match = _CSHARP_MAIN_RE.search(extra)
    if not match:
        return None
    entry = "SonderChecksMain_%s" % secrets.token_hex(8)
    renamed = extra[:match.start(1)] + entry + extra[match.end(1):]
    wrapper = (
        "\ninternal static class SonderChecksEntry_%(tag)s {\n"
        "    public static int Main(string[] args) {\n"
        "        System.Reflection.MethodInfo target = null;\n"
        "        foreach (System.Type type in typeof(SonderChecksEntry_%(tag)s).Assembly.GetTypes()) {\n"
        "            System.Reflection.MethodInfo found = type.GetMethod(\"%(entry)s\",\n"
        "                System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.Public\n"
        "                | System.Reflection.BindingFlags.NonPublic);\n"
        "            if (found != null) { target = found; break; }\n"
        "        }\n"
        "        object result;\n"
        "        try {\n"
        "            result = target.Invoke(null, target.GetParameters().Length == 0\n"
        "                ? null : new object[] { args });\n"
        "        } catch (System.Reflection.TargetInvocationException error) {\n"
        "            System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(\n"
        "                error.InnerException).Throw();\n"
        "            throw;\n"
        "        }\n"
        "        int code = 0;\n"
        "        System.Threading.Tasks.Task task = result as System.Threading.Tasks.Task;\n"
        "        if (task != null) {\n"
        "            task.GetAwaiter().GetResult();\n"
        "            System.Threading.Tasks.Task<int> valued = task as System.Threading.Tasks.Task<int>;\n"
        "            if (valued != null) code = valued.Result;\n"
        "        } else if (result is int) {\n"
        "            code = (int)result;\n"
        "        }\n"
        "        System.Console.Out.Write(%(marker)s);\n"
        "        System.Console.Out.Flush();\n"
        "        return code;\n"
        "    }\n"
        "}\n"
    ) % {"tag": entry[-16:], "entry": entry, "marker": _sentinel_literal(sentinel)}
    return "%s\n\n%s\n%s" % (code, renamed, wrapper)


def _run_javascript(code, extra, timeout, execute):
    node = shutil.which("node")
    if not node:
        return _missing("node")
    sentinel = _checks_sentinel() if extra and execute else ""
    if sentinel:
        src = _javascript_checked_source(code, extra, sentinel)
    else:
        src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        ok, out = _run_cmd([node, "--check", path], timeout)
        if not ok or not execute:
            return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
        return _finish_checked_run(_run_cmd([node, path], timeout), sentinel)
    finally:
        os.unlink(path)


def _run_powershell(code, extra, timeout, execute):
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if not exe:
        return _missing("pwsh/powershell")
    sentinel = _checks_sentinel() if extra and execute else ""
    if sentinel:
        src = _powershell_checked_source(code, extra, sentinel)
    else:
        src = code + (("\n\n" + extra) if extra else "")
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
        if not execute:
            # "compiled" previously meant only that the file was written, so a
            # syntactically broken script reported success. Parse it for real:
            # PSParser.Tokenize reports parse errors without running anything.
            checker = (
                "$errors = $null; "
                "[void][System.Management.Automation.PSParser]::Tokenize("
                "(Get-Content -Raw -LiteralPath '%s'), [ref]$errors); "
                "if ($errors.Count -gt 0) { "
                "$errors | ForEach-Object { Write-Output $_.Message }; exit 1 }"
                % path.replace("'", "''")
            )
            ok, out = _run_cmd(
                [exe, "-NoProfile", "-NonInteractive", "-Command", checker],
                timeout,
            )
            if ok:
                return True, "compiled"
            return False, ("compile failed\n" + out).strip()
        return _finish_checked_run(
            _run_cmd([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path], timeout),
            sentinel,
        )
    finally:
        os.unlink(path)


def _run_cpp(code, extra, timeout, execute):
    compiler = shutil.which("g++") or shutil.which("clang++") or shutil.which("cl")
    if not compiler:
        return _missing("g++/clang++/cl")
    sentinel = _checks_sentinel() if extra and execute else ""
    src = _cpp_checked_source(code, extra, sentinel) if sentinel else None
    if src is None:
        # Checks without an ``int main`` cannot be instrumented.
        sentinel = ""
        src = code + (("\n\n" + extra) if extra else "")
    with tempfile.TemporaryDirectory() as td:
        source = os.path.join(td, "main.cpp")
        exe = os.path.join(td, "main.exe" if os.name == "nt" else "main")
        with open(source, "w", encoding="utf-8") as f:
            f.write(src)
        if os.path.basename(compiler).lower() == "cl.exe":
            # /std:c++17 has to be explicit: MSVC still defaults to C++14, so
            # without it a snippet that compiled through code_runner.run_code
            # (which passes the flag) failed here with a C++14 diagnostic and
            # no hint that a sibling path would have built it. The g++/clang++
            # branch below has always pinned the same standard.
            ok, out = _run_cmd(
                [compiler, "/nologo", "/EHsc", "/std:c++17", source, "/Fe:" + exe],
                timeout,
                cwd=td,
            )
        else:
            ok, out = _run_cmd([compiler, "-std=c++17", source, "-o", exe], timeout, cwd=td)
        if not ok or not execute:
            return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
        return _finish_checked_run(_run_cmd([exe], timeout, cwd=td), sentinel)


def _run_csharp(code, extra, timeout, execute):
    compiler = shutil.which("csc")
    dotnet = shutil.which("dotnet")
    sentinel = _checks_sentinel() if extra and execute else ""
    src = _csharp_checked_source(code, extra, sentinel) if sentinel else None
    if src is None:
        # Checks without a ``Main`` cannot be instrumented.
        sentinel = ""
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
            return _finish_checked_run(_run_cmd([exe], timeout, cwd=td), sentinel)
        if dotnet:
            project = os.path.join(td, "App.csproj")
            with open(project, "w", encoding="utf-8") as f:
                f.write('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup></Project>')
            ok, out = _run_cmd([dotnet, "build", project, "--nologo", "-v:q"], timeout, cwd=td)
            if not ok or not execute:
                return (ok, "compiled" if ok else ("compile failed\n" + out).strip())
            return _finish_checked_run(
                _run_cmd([dotnet, "run", "--project", project, "--no-build"], timeout, cwd=td),
                sentinel,
            )
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
        # A job may fail because a phase exceeded its timeout. For compiled
        # languages the timeout is enforced per phase (compile, then run), so
        # the total `seconds` below can legitimately exceed the per-phase
        # budget without any single phase timing out. Flag genuine timeouts
        # explicitly so the formatter can distinguish them from other failures
        # and show the budget — otherwise "9.1s elapsed / timed out after 8s"
        # reads as a self-contradiction when the two are different clocks.
        timed_out = (not ok) and "timed out after" in (out or "")
        return {
            "index": index,
            "name": job["name"],
            "language": job["language"],
            "ok": bool(ok),
            "output": out,
            "seconds": round(time.time() - started, 3),
            "timed_out": timed_out,
            "timeout": job["timeout"],
        }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(one, i, job) for i, job in enumerate(normalized)]
        for future in as_completed(futures):
            result = future.result()
            results[result["index"]] = result
    return results


def format_code_jobs(results):
    passed = sum(1 for r in results if r.get("ok"))
    timed_out = sum(1 for r in results if r.get("timed_out"))
    header = "parallel code jobs: %d/%d passed" % (passed, len(results))
    if timed_out:
        header += " (%d timed out)" % timed_out
    lines = [header]
    compiled_langs = {"cpp", "csharp", "javascript"}
    for r in results:
        if r.get("timed_out"):
            # Show the enforced budget next to the total elapsed so the two
            # are reconcilable. For compiled languages the budget is per phase
            # (compile and run are timed separately), so total elapsed can
            # exceed it — say so rather than leave the reader to guess.
            budget = r.get("timeout")
            scope = "s/phase" if r.get("language") in compiled_langs else "s limit"
            status = "TIMEOUT %s%s" % (budget, scope) if budget else "TIMEOUT"
        else:
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
